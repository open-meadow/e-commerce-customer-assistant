### This is the overall goal of the project ###
E-Commerce Customer Support Assistant

E-commerce platforms receive large volumes of queries about orders, returns, payments, and policies. Human agents alone are slow and costly to scale. You will build an AI assistant that can resolve common issues autonomously using LLMs, RAG, and simple tool calls, with clear fallbacks when it cannot help.

Objectives

    Identify and scope key customer issue types (orders, returns, payments, FAQs).
    Design conversation flows, including clarification, fallback, and escalation.
    Implement an LLM-based assistant with RAG over FAQs/policies.
    Integrate simple “tools” (mock APIs) for order status, returns, refunds.
    Evaluate accuracy, usefulness, and safety, and iterate on prompts and flows.

Core Concepts & Techniques

    Large Language Models (LLMs) via API or open-source.
    Prompt engineering and system prompting for role, tone, and guardrails.
    RAG with embeddings + vector database over help-center docs.
    Agentic logic and tool / function calling (mock order/returns APIs).
    Basic evaluation of responses (relevance, hallucinations, safe handling).
    Simple deployment / UI for interactive demo.

Suggested Tools / Frameworks

    Backend & Language: Python
    LLM: Any LLM via API or open-source model
    Vector Store: Any vector database or embedding-based store
    Orchestration / Agent Layer: Generic LLM orchestration / agent framework
    UI (recommended): Simple web UI using a web framework + basic frontend
    Storage: JSON/CSV or simple DB for synthetic queries and logs

Data & Resources

    Knowledge Base:

        Synthetic or curated e-commerce FAQs, policies, shipping/return rules, and sample product descriptions (PDF/text/Markdown).

    Customer Query Data (Synthetic):

        Self-created or LLM-generated queries (e.g., “Where is my order?”, “I want a refund”) labeled with intents.

    Mock Backend “Tools”:

        Functions or endpoints such as:

            get_order_status(order_id)

            create_return_request(order_id, reason)

            get_refund_policy()

Expected Deliverables

    Working Prototype / MVP

        Interactive chatbot (web or CLI) that:

            Handles core customer intents

            Uses RAG for policy/FAQ answers

            Calls at least 2–3 mock tools (order status, returns, refunds)

            Has clear fallback / “escalate to human” behavior.

    Code Repository

        Organized codebase with clear structure.
