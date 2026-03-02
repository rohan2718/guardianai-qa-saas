import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import app, db
from models import TestRun, PageResult
from sqlalchemy import func


def backfill():
    with app.app_context():
        completed_runs = (
            TestRun.query
            .filter(TestRun.status == "completed")
            .order_by(TestRun.id.desc())
            .all()
        )

        print(f"Found {len(completed_runs)} completed runs to check.")
        updated = 0
        skipped = 0

        for run in completed_runs:
            pages = PageResult.query.filter_by(run_id=run.id).all()
            if not pages:
                skipped += 1
                continue

            def avg(vals):
                clean = [v for v in vals if v is not None]
                return round(sum(clean) / len(clean), 1) if clean else None

            # Compute component averages
            perf  = avg([p.performance_score   for p in pages])
            a11y  = avg([p.accessibility_score  for p in pages])
            sec   = avg([p.security_score       for p in pages])
            func  = avg([p.functional_score     for p in pages])
            ui    = avg([p.ui_form_score         for p in pages])
            total_broken = sum((p.broken_links_count or 0) for p in pages)

            # Only update if any column needs fixing
            needs_update = (
                (run.avg_performance_score   is None and perf  is not None) or
                (run.avg_accessibility_score is None and a11y  is not None) or
                (run.avg_security_score      is None and sec   is not None) or
                (run.avg_functional_score    is None and func  is not None) or
                (run.avg_ui_form_score       is None and ui    is not None) or
                (run.total_broken_links      == 0 and total_broken > 0)
            )

            if needs_update:
                run.avg_performance_score   = perf  or run.avg_performance_score
                run.avg_accessibility_score = a11y  or run.avg_accessibility_score
                run.avg_security_score      = sec   or run.avg_security_score
                run.avg_functional_score    = func  or run.avg_functional_score
                run.avg_ui_form_score       = ui    or run.avg_ui_form_score
                run.total_broken_links      = total_broken
                updated += 1
                print(f"  [run {run.id}] {run.target_url[:60]} → perf={perf} a11y={a11y} sec={sec} broken={total_broken}")
            else:
                skipped += 1

        try:
            db.session.commit()
            print(f"\n✅ Done. Updated: {updated}, Skipped (already ok or no pages): {skipped}")
        except Exception as e:
            db.session.rollback()
            print(f"\n❌ Commit failed: {e}")
            raise


if __name__ == "__main__":
    backfill()