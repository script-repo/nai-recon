"""ambientq: an on-prem ambient quality agent for LLM agents.

Ingests OpenTelemetry traces, grades sampled conversations with an LLM served
on Nutanix Enterprise AI (or any OpenAI-compatible endpoint), deduplicates
failures into insights and proposes harness patches for human approval.
"""

__version__ = "0.1.0"
