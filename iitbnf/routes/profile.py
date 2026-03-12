"""
routes/profile.py — /profile/<id> and /profile/<id>/pdf
"""
import time
from datetime import date, datetime
from flask import Blueprint, render_template, request, make_response
from auth import staff_required, is_full_access
from utils import run_parallel, safe_dict
from models.staff import (get_360_appraisal, get_committee_score, get_person, get_attendance_stats, get_equipment_stats,
                           get_project_data, get_monthly_reports, get_committee_involvement,
                           get_permissions, get_self_appraisal, get_staff_training, get_profile_tracking,
                           get_anomalies, get_attendance_trend, get_comparative_stats,
                           get_available_years, get_performance_rating, get_objectives)
from models.ai import generate_staff_report

bp = Blueprint("profile", __name__)


@bp.route("/profile/<int:member_id>")
@staff_required
def profile(member_id):
    start_total = time.time()

    year         = request.args.get("year", type=int) or date.today().year
    avail_years  = get_available_years(member_id=member_id)
    full_access  = is_full_access()  # evaluate before threads — session unavailable inside threads

    data = run_parallel({
        "person":      lambda: get_person(member_id),
        "attendance":  lambda: get_attendance_stats(member_id, year),
        "equipment":   lambda: get_equipment_stats(member_id, year),
        "projects":    lambda: get_project_data(member_id),
        "monthly":     lambda: get_monthly_reports(member_id, year),
        "committees":  lambda: get_committee_involvement(member_id),
        "permissions": lambda: get_permissions(member_id),
        "training":    lambda: get_staff_training(member_id, year),
        "tracking":    lambda: get_profile_tracking(member_id, year) if full_access else [],
        'self_appraisal': lambda: get_self_appraisal(member_id) if full_access else [],
        'appraisal_360':  lambda: get_360_appraisal(member_id) if full_access else [],
        'objectives':      lambda: get_objectives(member_id) if full_access else [],
        'perf_rating':     lambda: get_performance_rating(member_id) if full_access else [],
        'committee_score': lambda: get_committee_score(member_id) if full_access else [],
    })

    if not data.get("person"):
        return render_template("not_found.html", member_id=member_id), 404

    person   = data["person"]
    att      = data.get("attendance", {})
    equip    = data.get("equipment",  {})
    projects = data.get("projects",   {})
    self_appraisal = structure_appraisal(data.get('self_appraisal', []))
    appraisal_360  = structure_360(data.get('appraisal_360', []))
    perf_rating    = data.get('perf_rating', [])  # already structured, just sort by year
    objectives     = structure_appraisal(data.get('objectives', []))  # same structure as self
    anomalies  = get_anomalies(member_id, att, equip) if att and equip else []
    trend      = get_attendance_trend(member_id)
    comparison = get_comparative_stats(member_id, att, equip) if att and equip else {}
    person_safe = safe_dict(person)
    ai_summary = generate_staff_report(member_id=member_id, audience="management").get("report", "")
    raw_committee = data.get('committee_score') or []
    committee_scores = {}
    for r in raw_committee:
        cycle = r['review_name']
        if cycle not in committee_scores:
            committee_scores[cycle] = []
        committee_scores[cycle].append(r)
    total_ms = round((time.time() - start_total) * 1000, 2)
    html_content = render_template("profile.html",
        person=person_safe, att=att, appr={}, equip=equip, projects=projects,
        monthly=data.get("monthly", []), committees=data.get("committees", []),
        permissions=data.get("permissions", []),
        anomalies=anomalies, trend=trend, comparison=comparison,
        ai_summary=ai_summary,
        training=data.get("training", []),
        tracking=data.get("tracking", []),
        full_access=full_access,
        selected_year=year, avail_years=avail_years,
        member_id=member_id,
        self_appraisal=self_appraisal,
        appraisal_360=appraisal_360,
        objectives=objectives,
        perf_rating=perf_rating,
        committee_score=committee_scores,
    )
    response = make_response(html_content)
    response.headers["X-Total-Time"] = f"{total_ms}ms"
    return response


@bp.route("/profile/<int:member_id>/pdf")
@staff_required
def profile_pdf(member_id):
    import traceback
    try:
        data = run_parallel({
            "person":      lambda: get_person(member_id),
            "attendance":  lambda: get_attendance_stats(member_id),
            "equipment":   lambda: get_equipment_stats(member_id),
            "projects":    lambda: get_project_data(member_id),
            "monthly":     lambda: get_monthly_reports(member_id),
            "committees":  lambda: get_committee_involvement(member_id),
            "permissions": lambda: get_permissions(member_id),

        })
        if not data.get("person"):
            return render_template("not_found.html", member_id=member_id), 404

        person   = data["person"]
        att      = data.get("attendance", {})
        equip    = data.get("equipment",  {})
        projects = data.get("projects",   {})
        anomalies  = get_anomalies(member_id, att, equip) if att and equip else []
        trend      = get_attendance_trend(member_id)
        comparison = get_comparative_stats(member_id, att, equip) if att and equip else {}
        now        = datetime.now().strftime("%d %b %Y, %I:%M %p")

        html_content = render_template("profile_pdf.html",
            person=safe_dict(person), att=att, appr={}, equip=equip, projects=projects,
            monthly=data.get("monthly", []), committees=data.get("committees", []),
            permissions=data.get("permissions", []),
            anomalies=anomalies, trend=trend, comparison=comparison,
            ai_summary="(AI summary omitted in PDF export)",
            member_id=member_id, now=now,
        )

        from weasyprint import HTML
        try:
            pdf_bytes = HTML(string=html_content, base_url=request.host_url).write_pdf()
        except TypeError:
            from weasyprint.text.fonts import FontConfiguration
            pdf_bytes = HTML(string=html_content, base_url=request.host_url).write_pdf(
                font_config=FontConfiguration()
            )

        response = make_response(pdf_bytes)
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = f'attachment; filename="IITBNF_Profile_{member_id:04d}.pdf"'
        return response

    except Exception as e:
        traceback.print_exc()
        return f"PDF generation failed: {e}", 500
import re

def extract_year(name):
    m = re.search(r'\d{4}', name)
    return int(m.group()) if m else 0

def structure_appraisal(rows):
    """Group rows into {review_name: {ratings: [], comments: []}}"""
    cycles = {}
    for r in (rows or []):
        cycle = r['review_name']
        if cycle not in cycles:
            cycles[cycle] = {'ratings': [], 'comments': []}
        if r['type_of_field'] == 'radio':
            cycles[cycle]['ratings'].append({
                'field_name': r['field_name'],
                'value': r['value'],
                'order': r['order_of_display']
            })
        else:
            if r['value'].strip():  # skip blank text responses
                cycles[cycle]['comments'].append({
                    'field_name': r['field_name'],
                    'value': r['value'],
                    'order': r['order_of_display']
                })
    # Sort cycles by year descending
    return dict(sorted(cycles.items(), key=lambda x: extract_year(x[0]), reverse=True))

def structure_360(rows):
    """Same as above but average ratings across multiple reviewers."""
    from collections import defaultdict
    cycles = {}
    rating_buckets = defaultdict(list)  # (cycle, field_name) -> [values]

    for r in (rows or []):
        cycle = r['review_name']
        if cycle not in cycles:
            cycles[cycle] = {'ratings': [], 'comments': []}
        key = (cycle, r['field_name'])
        if r['type_of_field'] == 'radio':
            try:
                rating_buckets[key].append(float(r['value']))
            except ValueError:
                pass
        else:
            if r['value'].strip():
                cycles[cycle]['comments'].append({
                    'field_name': r['field_name'],
                    'value': r['value'],
                    'order': r['order_of_display']
                })

    # Average the ratings
    for (cycle, field_name), values in rating_buckets.items():
        cycles[cycle]['ratings'].append({
            'field_name': field_name,
            'value': round(sum(values) / len(values), 1),
            'reviewer_count': len(values)
        })

    return dict(sorted(cycles.items(), key=lambda x: extract_year(x[0]), reverse=True))