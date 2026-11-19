# Rebuilding the capstone deck

`build_deck.py` regenerates `docs/Capstone_Presentation.pptx` (23 slides) from the course PowerPoint template.

```bash
python docs/deck/build_deck.py --template path/to/Capstone_PPT_Template.pptx
python docs/deck/build_deck.py --template path/to/Capstone_PPT_Template.pptx --output build/deck.pptx
```

Run it from anywhere; paths are resolved relative to the repository. `--template` is required. `--output` defaults to
`docs/Capstone_Presentation.pptx`, so omit it only when you mean to overwrite the committed deck.

## Optional tooling (not an application dependency)

- **python-pptx 1.0.2** builds the deck. It also installs Pillow (12.0.0 was used).
- **Pillow** is needed only if you make contact sheets of rendered slides. The script itself does not import it.
- Neither package is in `pyproject.toml` and neither should be added. One way to run without touching the project
  environment:

```bash
uv run --no-project --with python-pptx==1.0.2 python docs/deck/build_deck.py --template path/to/template.pptx
```

## The template is not in this repository

The template is the course's own file and carries the BIA logo and confidentiality footer. Obtain it from the course
and pass it with `--template`. The footer wording and logo are used unchanged; whether that exact wording is
authorized for this submission has not been confirmed and is flagged for review.

## Limitations

- **Evaluation figures are hardcoded** in the script (the chart on the evaluation-results slide and the tiles such as
  157/158 and 55/57). They are **not** read from `docs/evaluation/results_summary.json`. If the evaluation changes,
  edit the script by hand and re-check every number against that file.
- **Rendering and visual checks are Windows-only.** Exporting slides to images was done with Microsoft PowerPoint
  (COM automation) and then inspected by eye. No render script is included, so layout, clipping and chart checks are
  manual.
- The script contains the deck content (slide text, speaker notes, layout). The deck cannot be regenerated without
  python-pptx and the template.
- A rebuild was verified to match the committed deck in slide count, all text, chart series and categories,
  speaker notes and the properties below. Binary byte identity is not expected.

## Confirmed deck details

- Slide 1 names: `Rukum Maurya | Rohan Varughese`.
- Creator: `Rukum Maurya; Rohan Varughese`. Last modified by: `Rukum Maurya`.
- Repository URL: `github.com/open-meadow/e-commerce-customer-assistant`.
