"""
Renders an AssessmentResult as a downloadable PDF report -- scores,
findings, and remediation, in the same shape a reader would expect from
the webapp's own result pages. Uses fpdf2 (pure Python, no system
dependencies) so it needs nothing extra in the Docker image.
"""
from __future__ import annotations

from fpdf import FPDF

from ai_readiness_agent.assessment.models import AssessmentResult

_MARGIN = 15
_PAGE_WIDTH = 210 - 2 * _MARGIN  # A4 minus margins, in mm


def _dimension_label(name: str) -> str:
    return name.replace("_", " ").title().replace("Ai ", "AI ")


def generate_pdf_report(result: AssessmentResult) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_margin(_MARGIN)
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=_MARGIN)

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, "AI Readiness Assessment Report", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(90, 90, 90)
    meta_lines = [
        f"Assessment ID: {result.assessment_id}",
        f"Use case: {result.use_case}    Environment: {result.environment_id}",
        f"Generated: {result.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    for line in meta_lines:
        pdf.cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    # --- hero stats ---
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(
        0, 10,
        f"Overall score: {round(result.overall_score)}/100   "
        f"({result.readiness_level.value})   "
        f"Projected: {round(result.projected_score)}/100",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(2)

    # --- dimension scores ---
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Dimension Scores", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for d in result.dimension_scores:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(
            0, 6,
            f"{_dimension_label(d.name)} -- {round(d.score)}/100 (weight {round(d.weight * 100)}%)",
            new_x="LMARGIN", new_y="NEXT",
        )
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(_PAGE_WIDTH, 5, d.summary, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(1)
    pdf.ln(3)

    # --- findings ---
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, f"Findings ({len(result.findings)})", new_x="LMARGIN", new_y="NEXT")
    if not result.findings:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, "No findings.", new_x="LMARGIN", new_y="NEXT")
    for f in result.findings:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, f"[{f.severity.upper()}] {f.title}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(_PAGE_WIDTH, 5, f.impact, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(1)
    pdf.ln(3)

    # --- remediation ---
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Remediation Plan", new_x="LMARGIN", new_y="NEXT")
    if not result.remediation:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, "No remediation needed -- all dimensions scored 85+.", new_x="LMARGIN", new_y="NEXT")
    for r in result.remediation:
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(
            _PAGE_WIDTH, 6,
            f"- {r.action} (effort: {r.effort}, projected gain: +{r.projected_score_delta})",
            new_x="LMARGIN", new_y="NEXT",
        )

    return bytes(pdf.output())
