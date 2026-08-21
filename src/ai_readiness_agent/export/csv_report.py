"""
Renders an AssessmentResult as a downloadable, sectioned CSV -- metadata,
then dimension scores / findings / remediation as their own tables in one
file. Each section has its own header row since the columns genuinely
differ; a single flat schema would leave most cells empty most of the time.
"""
from __future__ import annotations

import csv
import io

from ai_readiness_agent.assessment.models import AssessmentResult


def _dimension_label(name: str) -> str:
    return name.replace("_", " ").title().replace("Ai ", "AI ")


def generate_csv_report(result: AssessmentResult) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["Assessment ID", result.assessment_id])
    writer.writerow(["Use Case", result.use_case])
    writer.writerow(["Environment", result.environment_id])
    writer.writerow(["Overall Score", round(result.overall_score)])
    writer.writerow(["Readiness Level", result.readiness_level.value])
    writer.writerow(["Projected Score", round(result.projected_score)])
    writer.writerow(["Generated At", result.generated_at.strftime("%Y-%m-%d %H:%M UTC")])
    writer.writerow([])

    writer.writerow(["Dimension Scores"])
    writer.writerow(["Name", "Score", "Weight %", "Summary"])
    for d in result.dimension_scores:
        writer.writerow([_dimension_label(d.name), round(d.score), round(d.weight * 100), d.summary])
    writer.writerow([])

    writer.writerow(["Findings"])
    writer.writerow(["Severity", "Type", "Title", "Impact", "Source"])
    for f in result.findings:
        writer.writerow([f.severity, f.type, f.title, f.impact, f.source or ""])
    writer.writerow([])

    writer.writerow(["Remediation"])
    writer.writerow(["Action", "Effort", "Projected Score Delta"])
    for r in result.remediation:
        writer.writerow([r.action, r.effort, r.projected_score_delta])

    return buf.getvalue()
