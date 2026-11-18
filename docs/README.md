# Documentation map

Start with the [project README](../README.md). This page says where everything else lives and which documents are current.

## Current

| Topic | Document |
|---|---|
| **Architecture** and the reasoning behind each control | [architecture.md](architecture.md) |
| **Current status** (one page) | [PROJECT_STATUS.md](PROJECT_STATUS.md) |
| **Technical handover** (file-level detail, safe live-evaluation procedure, maintenance) | [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) |
| **Evaluation**: the one authoritative report | [evaluation/EVALUATION_REPORT.md](evaluation/EVALUATION_REPORT.md) |
| Evaluation files index and earlier runs | [evaluation/README.md](evaluation/README.md) |
| **Archived raw evidence** (143 files, SHA-256 manifest; for audit) | [evaluation/corrective_v2/raw/MANIFEST.md](evaluation/corrective_v2/raw/MANIFEST.md) |
| **Localization review**: 104 sentences, 13 High priority awaiting fluent human review | [localization_review_hinglish_hindi.md](localization_review_hinglish_hindi.md), [.csv](localization_review_hinglish_hindi.csv) |
| **Install, run and demonstrate** | [../README.md](../README.md) quick start; [06_demo_runbook.md](06_demo_runbook.md) for Docker, scripted verification and troubleshooting |
| Contributor guidance | [../CLAUDE.md](../CLAUDE.md), [../AGENTS.md](../AGENTS.md) |

## Design records and contracts (kept in place: code and tests cite them)

| Document | Role |
|---|---|
| [01_database_design.md](01_database_design.md) | Approved database design |
| [02_database_implementation_plan.md](02_database_implementation_plan.md) | Database implementation plan and checkpoints |
| [03_backend_architecture.md](03_backend_architecture.md) | Backend design and implementation record |
| [04_frontend_handover.md](04_frontend_handover.md) | UI-to-backend contract |
| [07_retrieval_and_embeddings.md](07_retrieval_and_embeddings.md) | Retrieval and embeddings |

Source files under `app/` cite `docs/01` and `docs/03` by name, and a test pins the localization sheets at their current paths, which is
why these files were not moved.

## Presentation deck

[Capstone_Presentation.pptx](Capstone_Presentation.pptx), rebuilt with [deck/build_deck.py](deck/README.md). **It predates the final
corrective-v2 results** (it shows 2,158 tests and the earlier evaluation figures) and has not been regenerated. The older 13-slide
prototype deck was intentionally replaced; its text outline is in [history/presentation.md](history/presentation.md).

## Historical (superseded, kept for the engineering record)

Each file begins with a banner saying what replaced it.

| Document | Superseded by |
|---|---|
| [history/technical_report.md](history/technical_report.md) | the evaluation report and architecture |
| [history/architecture_prototype.md](history/architecture_prototype.md) | architecture.md |
| [history/README_before_corrective_v2.md](history/README_before_corrective_v2.md) | the project README (keeps the detailed voice and Docker setup notes) |
| [history/PROJECT_STATUS_2026-11-13.md](history/PROJECT_STATUS_2026-11-13.md) | PROJECT_STATUS.md |
| [history/AGENTS_before_corrective_v2.md](history/AGENTS_before_corrective_v2.md), [history/CLAUDE_before_corrective_v2.md](history/CLAUDE_before_corrective_v2.md) | the concise guides |
| [history/05_handover_person_b.md](history/05_handover_person_b.md) | PROJECT_STATUS.md |
| [history/00_project_discovery.md](history/00_project_discovery.md) | (origin of the requirements; retained) |
| [history/presentation.md](history/presentation.md) | the 23-slide deck |
| [history/ui_real_backend_plan.md](history/ui_real_backend_plan.md) | completed plan |
| [history/GOAL.md](history/GOAL.md) | the original course brief |

Internal planning notes that used to live in `.claude/plans/` were removed from the tree; they remain in Git history.
