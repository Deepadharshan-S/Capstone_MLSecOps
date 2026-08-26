-- Create lakefs database if it does not already exist
SELECT 'CREATE DATABASE lakefs'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'lakefs')\gexec

-- Create mlflow database if it does not already exist
SELECT 'CREATE DATABASE mlflow'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mlflow')\gexec
