# pdf2ja 参考文献改行・組版・長大ブロック翻訳の改良仕様書

## 1. 概要
PDFMathTranslate をフォークした `pdf2ja` において、学術論文の「参考文献（References）」セクションを `gpt-oss-120b` などの LLM で翻訳・組版する際に発生していた以下の課題を解決した。

1. **参考文献番号の直前で改行が入らず、前後の文献が1行に結合してしまう問題**
2. **LLM の出力トークン上限（max_tokens）超過により、参考文献の途中で翻訳が打ち切られる問題**（例：[1]〜[30] あるうちの [8] で途切れる）
3. **年号（例: `(2009)`）などのカッコ数字で誤って改行されてしまう誤爆問題**
4. **組版エンジンが `\n` を無視し、行末まで改行されない問題**
5. **改行挿入による行数増加に伴い、ページ下端を突き抜けて後半の文献が消失（クリップ）する問題**

---

## 2. 発生していた課題と根本原因

### 課題 A: 排版エンジンが `\n` を無視する仕様 (`converter.py`)
- **原因:** 元の組版ループでは `if brk and x + adv > x1 + 0.1 * size:`（右端の壁に当たったとき）しか改行（`x = x0; lidx += 1`）を行っておらず、テキスト内の改行文字 `\n` を通常スペース同様に無視して横に流していた。

### 課題 B: 長大ブロックによる LLM の生成中断・途切れ
- **原因:** [1]〜[30]（60行以上）の長大な参考文献ブロックを1回のリクエストで LLM に送信したため、`max_tokens` に達して [8] の途中で生成が中断し、欠落したテキストがそのままキャッシュされていた。

### 課題 C: 発行年 `(2009)` などの誤爆改行
- **原因:** 正規表現パターンに `|\(\d+\)`（カッコ数字）が含まれていたため、文献番号ではない年号 `(2009)` や `(2017)` の直前にも `\n` が挿入され、ジャーナル名や年号が途中で不自然に改行されていた。

### 課題 D: 枠外オーバーフローによる後半文献の消失
- **原因:** 改行を入れて行数が増えると、元の英語ブロックの高さ枠（`height`）を超過し、ページ最下部のマージン外に描画されて [26] 以降が見えなくなっていた。

---

## 3. 実装した改良内容

### 3.1 参考文献の改行正規表現の厳格化 (`high_level.py`)
年号の誤爆を防ぎ、角カッコ `[数字]` および 行頭の `数字.` のみに限定。
重複改行（`\n\n`）も単一改行（`\n`）へ正規化。

```python
def fix_reference_newlines(text: str) -> str:
    """参考文献の番号 [1], [2], 1., (1) などの直前に確実に改行を1つだけ入れる"""
    if not text:
        return text
    # 連続改行をあらかじめ1つに圧縮
    text = re.sub(r"\n+", "\n", text)
    # 年号 (2009) などの誤爆を防ぐため、角カッコ [数字] および 行頭数字. のみに限定
    ref_pattern = r"(?<!^)\s*(?=\[\d+\]|(?<=\n)\d+\.\s+)"
    res = re.sub(ref_pattern, "\n", text)
    return re.sub(r"\n+", "\n", res).strip()
```

### 3.2 長大参考文献ブロックの自動分割翻訳（チャンク化） (`high_level.py`)
参考文献が 4 件以上含まれる長大な段落の場合、5件単位で分割して LLM に送信し、結果を結合することで、トークン溢れによる途切れを防止。

```python
    original_translate = device.translator.translate

    def wrapped_translate(text_input: str, *args, **kwargs):
        fixed_in = fix_reference_newlines(text_input)
        lines_in = fixed_in.splitlines()
        ref_count = sum(1 for l in lines_in if re.match(r"^(\[\d+\]|\d+\.)", l.strip()))
        
        # 4件以上の参考文献を含む長大ブロックは分割翻訳で出力途切れを回避
        if ref_count >= 4:
            chunks = []
            chunk_size = 5
            for k in range(0, len(lines_in), chunk_size):
                sub = "\n".join(lines_in[k:k + chunk_size])
                if sub.strip():
                    chunks.append(original_translate(sub, *args, **kwargs))
            result = "\n".join(chunks)
        else:
            result = original_translate(fixed_in, *args, **kwargs)

        return fix_reference_newlines(result)

    device.translator.translate = wrapped_translate
```

### 3.3 組版エンジンにおける物理的改行の実装 (`converter.py`)
`ord(ch) == 10`（改行文字 `\n`）を検知した際、直前のバッファを出力し、カーソルを左端（`x = x0`）に戻して次行（`lidx += 1`）へ移行する処理を追加。

```python
                else:  # 文字処理
                    ch = new[ptr]
                    if ord(ch) == 10:  # \n 改行文字の検知
                        if cstk:
                            ops_vals.append({
                                "type": OpType.TEXT,
                                "font": fcur,
                                "size": size,
                                "x": tx,
                                "dy": 0,
                                "rtxt": raw_string(fcur, cstk),
                                "lidx": lidx
                            })
                            cstk = ""
                        x = x0
                        lidx += 1
                        ptr += 1
                        continue
```

### 3.4 行数増加時の自動フォント・行間リサイズ (`converter.py`)
改行が増えて段落の高さ枠（`height`）を超える場合、`line_height` を 0.75 まで詰め、それでも溢れる場合はフォントサイズ `size` を自動でスケールダウンして全行をページ枠内にピッタリ収容。

```python
            line_height = default_line_height

            # 行数が多くて元の枠の高さを超える場合は、line_height を 0.75 まで圧縮し、
            # それでも超える場合はフォントサイズ(size)も比例して自動縮小する
            while (lidx + 1) * size * line_height > height and line_height >= 0.75:
                line_height -= 0.03
            
            if (lidx + 1) * size * line_height > height:
                scale_ratio = height / ((lidx + 1) * size * line_height)
                size = max(size * scale_ratio, 5.0)
```

### 3.5 本文中の引用番号（インライン引用）の誤爆改行の防止 (`high_level.py`)
- **課題:**
  - `fix_reference_newlines` が本文中の引用（例: `0.27 eV [34] または 0.31 eV [35]`）に対しても改行を挿入してしまい、本文が不自然に途切れる問題が発生。
- **原因:**
  - 「参考文献リストの項目」と「本文中の引用番号」を判別せず、あらゆる `[\d+]` にマッチさせていたため。
- **対策:**
  - `is_reference_section(text)` ガード関数を追加。
  - その段落内に文献パターン（`[\d+] 人名/英字`）が 2 件以上あるか、または段落の先頭が `[\d+] 人名` で始まっている場合のみを「参考文献セクション」として認識し、通常の本文パラグラフでは改行挿入を一切スキップするよう改修。
- **検証結果:**
  - 1ページ目の本文テキストを確認したところ、`（0.27 eV [34] または 0.31 eV [35]）` が改行されず自然な1行として綺麗に描画されることを確認。

### 3.6 実行時コンソール出力（INFO / デバッグログ）の抑制と最適化
- **課題:**
  - 通常実行時に、ユーザーにとって不要な内部デバッグ情報や冗長なログが画面に大量出力されていた。
- **見直した出力内容と対策:**
  1. **`doclayout.py` (`OnnxModel.__init__`):**
     - `logger.info("ONNX Runtime providers: %s", self.model.get_providers())` を `logger.debug` に変更し、通常実行時のプロバイダ情報出力を抑制。
  2. **`pdf2ja.py` (CLI エントリーポイント):**
     - 生の引数ダンプ `print(parsed_args)` を `if parsed_args.debug:` 内に移動し、`--debug` 指定時のみ表示するよう変更。
  3. **`high_level.py` (`download_remote_fonts`):**
     - `logger.info(f"use font: {font_path}")` を `logger.debug` に変更。
- **効果:**
  - コマンド実行時、不要なログが消え、プログレスバーと進捗のみがスッキリ表示されるクリーンなコンソール画面となった。


---

## 4. 検証結果

- **対象文献:** `1708.07606.pdf` (ページ 7)
- **結果:**
  - [1] から [30] までの **全30件の参考文献が1つも欠落せず最後まで翻訳** された。
  - 発行年 `(2009)` などの誤爆改行がなくなり、`J. Am. Chem. Soc. 131 (2009) 6050–6051.` が綺麗に1行で繋がった。
  - 項目間の不自然な2段空行が解消され、1行＝1文献のコンパクトで美しいレイアウトでPDF（`1708.07606-mono.pdf`）内に完全収容された。
