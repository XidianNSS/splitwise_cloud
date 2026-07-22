from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.models import EdgeSession


def refresh_session_lease(db: Session, edge_session: EdgeSession, *, hours: int = 2) -> EdgeSession:
    now = datetime.utcnow()
    lease_expires_at = now + timedelta(hours=hours)
    edge_session.status = "active"
    edge_session.updated_at = now
    edge_session.last_active_at = now
    edge_session.expires_at = lease_expires_at
    edge_session.lease_expires_at = lease_expires_at
    db.add(edge_session)
    db.commit()
    db.refresh(edge_session)
    return edge_session
