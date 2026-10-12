"""Kernel package — hot-pluggable translation kernel registry."""

from pdf2ja.kernel.registry import KernelRegistry
from pdf2ja.kernel.legacy import LegacyKernel
from pdf2ja.kernel.precise import PreciseKernel

# Always register both kernels.
# PreciseKernel.is_available() returns False if submodule/venv not set up.
KernelRegistry.register(LegacyKernel())
KernelRegistry.register(PreciseKernel())

__all__ = ["KernelRegistry"]
