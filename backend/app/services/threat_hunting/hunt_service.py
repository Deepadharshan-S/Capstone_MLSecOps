import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.threat_hunting import HuntJob, SecurityEvent
from app.schemas.threat_hunting import HuntDefinition, HuntJobCreate, SecurityEventFilter

logger = logging.getLogger("threat_hunting.hunts")


class ThreatHuntService:
    """
    Manages historical threat hunting jobs and structured query execution.
    Strictly uses parameterized SQLAlchemy filters to prevent SQL injection.
    """

    def create_hunt_job(
        self,
        db: Session,
        hunt_in: HuntJobCreate,
        user_id: Optional[UUID] = None,
    ) -> HuntJob:
        """
        Creates a new pending hunt job.
        """
        job = HuntJob(
            name=hunt_in.name,
            hunt_definition=hunt_in.hunt_definition.model_dump(mode="json"),
            requested_by=user_id,
            status="PENDING",
            result_count=0,
            results_summary={},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def create_and_execute_hunt(
        self,
        db: Session,
        hunt_in: HuntJobCreate,
        user_id: Optional[UUID] = None,
    ) -> HuntJob:
        """
        Creates and immediately runs a historical hunt job.
        """
        job = self.create_hunt_job(db, hunt_in, user_id=user_id)
        return self.execute_hunt_job(db, job.id)

    def execute_hunt_job(
        self,
        db: Session,
        hunt_id: UUID,
    ) -> HuntJob:
        """
        Executes a historical threat hunt against stored security events safely.
        """
        job = db.get(HuntJob, hunt_id)
        if not job:
            raise ValueError(f"Hunt job {hunt_id} not found.")

        job.status = "RUNNING"
        job.started_at = datetime.now(timezone.utc)
        db.commit()

        try:
            defn = HuntDefinition(**job.hunt_definition)
            query = select(SecurityEvent)
            conditions = []

            # Time range is strictly required
            st = defn.start_time if defn.start_time.tzinfo else defn.start_time.replace(tzinfo=timezone.utc)
            et = defn.end_time if defn.end_time.tzinfo else defn.end_time.replace(tzinfo=timezone.utc)
            conditions.append(SecurityEvent.occurrence_time >= st)
            conditions.append(SecurityEvent.occurrence_time <= et)

            if defn.event_types:
                conditions.append(SecurityEvent.event_type.in_(defn.event_types))
            if defn.sources:
                conditions.append(SecurityEvent.source.in_(defn.sources))
            if defn.actor_id:
                conditions.append(SecurityEvent.actor_id == defn.actor_id)
            if defn.resource_id:
                conditions.append(SecurityEvent.resource_id == defn.resource_id)
            if defn.severities:
                upper_sevs = [s.upper() for s in defn.severities]
                conditions.append(SecurityEvent.severity.in_(upper_sevs))

            query = query.where(and_(*conditions)).order_by(SecurityEvent.occurrence_time.desc())
            events = list(db.execute(query.limit(defn.limit)).scalars().all())

            # Attribute filters if provided
            if defn.attribute_filters:
                filtered = []
                for ev in events:
                    attrs = ev.attributes or {}
                    if all(attrs.get(k) == v for k, v in defn.attribute_filters.items()):
                        filtered.append(ev)
                events = filtered

            # Calculate statistical hunt summary
            type_counts: dict[str, int] = {}
            actor_counts: dict[str, int] = {}
            sev_counts: dict[str, int] = {}
            for ev in events:
                type_counts[ev.event_type] = type_counts.get(ev.event_type, 0) + 1
                if ev.actor_id:
                    actor_counts[ev.actor_id] = actor_counts.get(ev.actor_id, 0) + 1
                sev_counts[ev.severity] = sev_counts.get(ev.severity, 0) + 1

            summary = {
                "total_matched": len(events),
                "event_type_distribution": type_counts,
                "actor_distribution": actor_counts,
                "severity_distribution": sev_counts,
                "top_actors": sorted(actor_counts.items(), key=lambda x: x[1], reverse=True)[:5],
            }

            job.status = "COMPLETED"
            job.completed_at = datetime.now(timezone.utc)
            job.result_count = len(events)
            job.results_summary = summary
            db.commit()
            db.refresh(job)
            return job
        except Exception as e:
            logger.error(f"Error executing hunt job {hunt_id}: {e}", exc_info=True)
            job.status = "FAILED"
            job.completed_at = datetime.now(timezone.utc)
            job.error_message = str(e)
            db.commit()
            db.refresh(job)
            return job

    def get_hunt_job(self, db: Session, hunt_id: UUID) -> Optional[HuntJob]:
        return db.get(HuntJob, hunt_id)

    def list_hunt_jobs(
        self,
        db: Session,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[HuntJob], int]:
        query = select(HuntJob)
        if status:
            query = query.where(HuntJob.status == status.upper())
        query = query.order_by(HuntJob.created_at.desc())

        count_query = select(func.count(HuntJob.id))
        if status:
            count_query = count_query.where(HuntJob.status == status.upper())
        total = db.execute(count_query).scalar_one()

        jobs = list(db.execute(query.offset(offset).limit(limit)).scalars().all())
        return jobs, total

    def get_hunt_results(
        self,
        db: Session,
        hunt_id: UUID,
    ) -> tuple[HuntJob, list[SecurityEvent]]:
        """
        Re-fetches the matched events for a completed hunt job.
        """
        job = self.get_hunt_job(db, hunt_id)
        if not job:
            raise ValueError(f"Hunt job {hunt_id} not found.")

        defn = HuntDefinition(**job.hunt_definition)
        st = defn.start_time if defn.start_time.tzinfo else defn.start_time.replace(tzinfo=timezone.utc)
        et = defn.end_time if defn.end_time.tzinfo else defn.end_time.replace(tzinfo=timezone.utc)

        conditions = [
            SecurityEvent.occurrence_time >= st,
            SecurityEvent.occurrence_time <= et,
        ]
        if defn.event_types:
            conditions.append(SecurityEvent.event_type.in_(defn.event_types))
        if defn.sources:
            conditions.append(SecurityEvent.source.in_(defn.sources))
        if defn.actor_id:
            conditions.append(SecurityEvent.actor_id == defn.actor_id)
        if defn.resource_id:
            conditions.append(SecurityEvent.resource_id == defn.resource_id)
        if defn.severities:
            upper_sevs = [s.upper() for s in defn.severities]
            conditions.append(SecurityEvent.severity.in_(upper_sevs))

        query = (
            select(SecurityEvent)
            .where(and_(*conditions))
            .order_by(SecurityEvent.occurrence_time.desc())
            .limit(defn.limit)
        )
        events = list(db.execute(query).scalars().all())
        if defn.attribute_filters:
            filtered = []
            for ev in events:
                attrs = ev.attributes or {}
                if all(attrs.get(k) == v for k, v in defn.attribute_filters.items()):
                    filtered.append(ev)
            events = filtered

        return job, events


hunt_service = ThreatHuntService()
