"""Seed data: sample emails generated together with their correct answers."""

from invoice_collector.seed.generator import (
    Renderer,
    SeedConfig,
    SeedData,
    SeedMessage,
    generate,
    load_messages,
    write_folder,
)

__all__ = [
    "Renderer",
    "SeedConfig",
    "SeedData",
    "SeedMessage",
    "generate",
    "load_messages",
    "write_folder",
]
