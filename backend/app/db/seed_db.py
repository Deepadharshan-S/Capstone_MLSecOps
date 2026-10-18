import os
import logging
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.user import User
from app.repositories import UserRepository
from app.core.security import hash_password, validate_password_strength

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed_db")


def seed():
    db = SessionLocal()
    try:
        user_repo = UserRepository(db)
        # Check if users already exist
        users_count = user_repo.count()
        if users_count > 0:
            logger.info("Database already seeded with users.")
            return

        is_production = settings.ENVIRONMENT.lower() in ("production", "prod")
        if is_production:
            admin_password = os.getenv("SEED_ADMIN_PASSWORD")
            if not admin_password:
                logger.warning(
                    "Database seeding with default development credentials skipped in production. "
                    "Set SEED_ADMIN_PASSWORD to provision an initial administrator safely."
                )
                return

            valid, err = validate_password_strength(admin_password)
            if not valid:
                raise ValueError(f"SEED_ADMIN_PASSWORD does not meet password policy: {err}")

            admin_user = os.getenv("SEED_ADMIN_USERNAME", "admin_user")
            admin_email = os.getenv("SEED_ADMIN_EMAIL", "admin@sentinelml.com")
            user = User(
                username=admin_user,
                email=admin_email,
                password_hash=hash_password(admin_password),
                role="admin",
                is_active=True,
            )
            db.add(user)
            db.commit()
            logger.info(f"Production database seeded with admin user '{admin_user}'.")
            return

        logger.info("Seeding database with default users for each role...")

        # Define users to seed (all passwords comply with the complexity policy)
        seed_data = [
            {
                "username": "admin_user",
                "email": "admin@sentinelml.com",
                "password": "AdminPassword123!",
                "role": "admin",
            },
            {
                "username": "ds_user",
                "email": "ds@sentinelml.com",
                "password": "DataScientist123!",
                "role": "data_scientist",
            },
            {
                "username": "mle_user",
                "email": "mle@sentinelml.com",
                "password": "MLEngineerPassword123!",
                "role": "ml_engineer",
            },
            {
                "username": "viewer_user",
                "email": "viewer@sentinelml.com",
                "password": "ViewerPassword123!",
                "role": "viewer",
            },
        ]

        for u in seed_data:
            user = User(
                username=u["username"],
                email=u["email"],
                password_hash=hash_password(u["password"]),
                role=u["role"],
                is_active=True,
            )
            db.add(user)
            logger.info(f"Created user '{u['username']}' with role '{u['role']}'.")

        db.commit()
        logger.info("Database seeding completed successfully.")

    except Exception as e:
        db.rollback()
        logger.error(f"Error seeding database: {e}")
        raise e
    finally:
        db.close()


if __name__ == "__main__":
    seed()
