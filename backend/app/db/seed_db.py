import logging
from app.db.session import SessionLocal
from app.models.user import User
from app.core.security import hash_password

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed_db")


def seed():
    db = SessionLocal()
    try:
        # Check if users already exist
        users_count = db.query(User).count()
        if users_count > 0:
            logger.info("Database already seeded with users.")
            return

        logger.info("Seeding database with default users for each role...")

        # Define users to seed (all passwords comply with the complexity policy)
        seed_data = [
            {
                "username": "admin_user",
                "email": "admin@mlsecops.com",
                "password": "AdminPassword123!",
                "role": "admin",
            },
            {
                "username": "ds_user",
                "email": "ds@mlsecops.com",
                "password": "DataScientist123!",
                "role": "data_scientist",
            },
            {
                "username": "mle_user",
                "email": "mle@mlsecops.com",
                "password": "MLEngineerPassword123!",
                "role": "ml_engineer",
            },
            {
                "username": "viewer_user",
                "email": "viewer@mlsecops.com",
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
