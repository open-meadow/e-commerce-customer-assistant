# AI-Powered E-Commerce Customer Support Assistant

An AI-powered customer support system designed to help e-commerce customers resolve common issues related to orders, deliveries, payments, returns, refunds, products, and support policies through a conversational interface.

The project combines **Large Language Models (LLMs), Retrieval-Augmented Generation (RAG), structured database queries, and controlled backend tools** to provide context-aware and reliable customer support.

## 1. Project Overview

E-commerce platforms receive large volumes of repetitive customer support queries, including:

- Where is my order?
- Has my payment been successful?
- Can I cancel my order?
- Am I eligible for a return?
- When will I receive my refund?
- What are the specifications of a product?
- How can I contact customer support?

This project aims to build an AI assistant that can understand these questions, retrieve relevant information, access authorized customer records, and guide customers through appropriate support workflows.

The system uses synthetic customer and transaction data. It does not connect to a real e-commerce platform.

## 2. Project Objectives

- Build a conversational customer support interface.
- Retrieve accurate answers from FAQs and policy documents using RAG.
- Retrieve product information from structured JSON data.
- Access customer-specific order and transaction information securely.
- Implement controlled backend tools for supported operations.
- Create support tickets when escalation is required.
- Prevent unauthorized access to another customer's information.
- Evaluate answer quality, retrieval performance, and tool execution reliability.

## 3. Project Scope

### Included in the MVP

| Component | Responsibility |
|---|---|
| Streamlit UI | Conversational customer support interface |
| Customer context | Simulated authenticated customer identity |
| SQLite database | Customers, orders, order items, returns, and support tickets |
| RAG pipeline | Retrieve information from FAQ and policy documents |
| Product lookup | Retrieve product details from `Products.json` |
| Backend tools | Controlled customer-data retrieval and support operations |
| LLM orchestration | Understand queries and coordinate retrieval and tools |
| Ticket escalation | Create support ticket records |
| Testing | Authorization, business logic, retrieval, and orchestration tests |

**Authentication note:** The MVP uses a simulated customer selector in Streamlit session state. This is for demonstrating authorization boundaries and is not production-grade authentication.

### Explicitly Out of Scope

- Full e-commerce marketplace frontend.
- Product catalogue browsing and shopping interface.
- Customer registration and onboarding.
- Shopping cart and checkout.
- Payment gateway integration.
- Real courier or shipping integrations.
- Real refund processing.
- Production identity and authentication infrastructure.

Voice-based support may be considered as a future enhancement after the MVP is completed and tested.

## 4. Technology Stack

| Area | Technology |
|---|---|
| Programming language | Python |
| User interface | Streamlit |
| Database | SQLite |
| Structured data | JSON |
| Knowledge base | Markdown documents |
| AI capabilities | LLM |
| Knowledge retrieval | Retrieval-Augmented Generation (RAG) |
| Testing | Pytest |
| Version control | Git and GitHub |

Specific LLM providers, embedding models, and vector storage technologies will be finalized during architecture and implementation.

## 5. High-Level Architecture

```text
                 CUSTOMER
                    |
                    v
             STREAMLIT CHAT UI
                    |
                    v
          CUSTOMER CONTEXT / SESSION
                    |
                    v
             AI ORCHESTRATOR
                    |
          +---------+----------+
          |         |          |
          v         v          v
       RAG       PRODUCT     BACKEND
      ENGINE     LOOKUP       TOOLS
          |         |          |
          v         v          v
      FAQ /      Products    SQLite
      Policies   JSON        Database
          |         |          |
          +---------+----------+
                    |
                    v
              LLM RESPONSE
                    |
                    v
               CUSTOMER
```

The architecture will evolve as individual components are implemented and tested.

## 6. Security and Reliability Principles

Security is a core requirement, not an optional enhancement.

- Customer identity must originate from trusted application context.
- Customer IDs must never be trusted from user messages or LLM-generated arguments.
- Customer-specific database queries must enforce ownership filters.
- Customers must never access another customer's orders, returns, or tickets.
- The LLM must not receive unrestricted database or SQL access.
- State-changing operations must go through controlled backend functions.
- The assistant must never claim an operation succeeded without backend confirmation.
- Sensitive customer information must not be unnecessarily exposed in logs.
- Retrieved documents and user messages must be treated as untrusted input.

## 7. Project Structure

```text
ecommerce-support-assistant/
│
├── app/
│   ├── db/
│   ├── tools/
│   ├── rag/
│   ├── orchestrator/
│   └── ui/
│
├── data/
│   ├── source/
│   └── generated/
│
├── docs/
│
├── tests/
│
├── .gitignore
├── PROJECT_STATUS.md
├── README.md
└── requirements.txt
```

## 8. Dataset

The project uses a provided synthetic dataset containing:

- `FAQ.md` — common customer questions and answers.
- `Policies.md` — shipping, returns, refunds, cancellation, and warranty policies.
- `Products.json` — structured product information.
- `customer_queries_sample.csv` — sample customer support queries.
- `README_DATASET.md` — dataset documentation.

Synthetic transactional records will be generated for customers, orders, order items, returns, and support tickets.

The supplied FAQ and policy documents are the source of truth for the MVP's support rules.

## 9. Development Methodology

The project will be developed incrementally through approved checkpoints.

1. Project discovery and requirements.
2. Database design.
3. Database implementation and authorization testing.
4. Backend service and tool development.
5. RAG pipeline implementation.
6. LLM orchestration.
7. Streamlit interface.
8. Integration and end-to-end testing.
9. Evaluation and documentation.
10. Final demonstration and deployment preparation.

Each checkpoint must be reviewed and tested before moving to the next.

## 10. Running the Project

From the repository root, start the Streamlit chat with:

```bash
uv run streamlit run app/ui/streamlit_app.py
```

Choose C101 or C102 in the sidebar to use the simulated login. The UI currently uses a local fake backend with canned replies and demo return-choice and confirmation flows; it is not connected to live order, return, policy, or ticket data. Confirming a demo action does not submit a real request.

### Run with Docker

From the repository root, run:

```bash
docker compose up --build
```

Open `http://localhost:8501`. The Compose service stores its data and vector-store files in Docker-managed volumes. The current demo uses a fake backend and does not require an API key.

## 11. Disclaimer

This is an educational capstone project using synthetic data. It is not connected to any real e-commerce company, customer account, payment gateway, or shipping provider.