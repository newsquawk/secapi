import os
from typing import Optional
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool, PoolError
from fastapi import HTTPException

from config import APP_ENV, logger

INTERNAL_ERROR_DETAIL = "An internal error occurred. Please try again later."

DB_POOL_MIN_CONN = int(os.getenv("DB_POOL_MIN_CONN", "4"))
DB_POOL_MAX_CONN = int(os.getenv("DB_POOL_MAX_CONN", "20"))
db_pool: Optional[ThreadedConnectionPool] = None


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def validate_production_config() -> None:
    if APP_ENV == "production":
        _require_env("DB_PASSWORD")
    if not os.getenv("CORS_ORIGINS"):
        logger.warning("CORS_ORIGINS is not set; cross-origin requests will be blocked.")


def _get_db_connection_params() -> dict:
    if APP_ENV == "production":
        db_password = _require_env("DB_PASSWORD")
    else:
        db_password = os.getenv("DB_PASSWORD", "")

    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "database": os.getenv("DB_NAME", "sec"),
        "user": os.getenv("DB_USER", "postgres"),
        "password": db_password,
        "port": os.getenv("DB_PORT", "5432"),
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
        "sslmode": os.getenv("DB_SSLMODE", "prefer"),
        "keepalives": 1,
        "keepalives_idle": int(os.getenv("DB_KEEPALIVE_IDLE", "30")),
    }


def init_db_pool() -> None:
    """Initializes the ThreadedConnectionPool on application startup."""
    global db_pool
    if db_pool is not None:
        return
    try:
        params = _get_db_connection_params()
        db_pool = ThreadedConnectionPool(
            minconn=DB_POOL_MIN_CONN,
            maxconn=DB_POOL_MAX_CONN,
            **params,
        )
        logger.info(
            f"Initialized database connection pool (min={DB_POOL_MIN_CONN}, max={DB_POOL_MAX_CONN})"
        )
    except psycopg2.Error:
        logger.error("Failed to initialize database connection pool", exc_info=True)
        if APP_ENV == "production":
            raise


def close_db_pool() -> None:
    """Closes all connections in the pool on application shutdown."""
    global db_pool
    if db_pool is not None:
        try:
            db_pool.closeall()
            logger.info("Closed all connections in database pool")
        except Exception:
            logger.error("Error closing database connection pool", exc_info=True)
        finally:
            db_pool = None


def get_db_connection():
    """
    Establishes and returns a new direct PostgreSQL database connection.
    Maintained for standalone operations and fallbacks.
    """
    try:
        params = _get_db_connection_params()
        return psycopg2.connect(**params)
    except psycopg2.Error:
        logger.error("Database connection failed", exc_info=True)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)


def _is_connection_healthy(conn) -> bool:
    """Verifies that a database connection is open and capable of executing queries."""
    if conn is None or getattr(conn, "closed", 1) != 0:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
        return True
    except Exception:
        return False


def get_db_cursor():
    """
    Dependency that yields a database cursor backed by a connection from the pool
    and ensures the connection is cleanly returned with rollback on error.
    Automatically validates connection liveness and auto-heals dead pool sockets.
    """
    global db_pool
    conn = None
    cursor = None
    is_pooled = False

    # Attempt to initialize pool if not already initialized
    if db_pool is None:
        try:
            init_db_pool()
        except Exception:
            pass

    if db_pool is not None:
        try:
            # Try acquiring a healthy connection from the pool
            # Prune any dead sockets (e.g. if PostgreSQL was restarted)
            for _ in range(5):
                candidate = db_pool.getconn()
                if _is_connection_healthy(candidate):
                    conn = candidate
                    is_pooled = True
                    break
                else:
                    # Discard dead connection from pool
                    try:
                        db_pool.putconn(candidate, close=True)
                    except Exception:
                        pass

            # If all pool candidates were dead, re-initialize pool and fallback
            if conn is None:
                logger.warning("All pooled connections were dead; re-initializing pool")
                try:
                    init_db_pool()
                    candidate = db_pool.getconn()
                    if _is_connection_healthy(candidate):
                        conn = candidate
                        is_pooled = True
                    else:
                        db_pool.putconn(candidate, close=True)
                        conn = get_db_connection()
                        is_pooled = False
                except Exception:
                    conn = get_db_connection()
                    is_pooled = False

        except PoolError:
            logger.error("Database connection pool exhausted", exc_info=True)
            raise HTTPException(
                status_code=503,
                detail="Database connection pool exhausted. Please retry shortly.",
            )
        except psycopg2.Error:
            logger.error("Failed to acquire connection from pool", exc_info=True)
            raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)
    else:
        # Fallback to direct connection if pool could not be initialized
        conn = get_db_connection()

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        yield cursor
    except Exception:
        if conn and not getattr(conn, "closed", 1):
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if cursor and not getattr(cursor, "closed", 1):
            try:
                cursor.close()
            except Exception:
                pass
        if conn:
            if is_pooled and db_pool is not None:
                if getattr(conn, "closed", 1):
                    try:
                        db_pool.putconn(conn, close=True)
                    except Exception:
                        pass
                else:
                    try:
                        conn.rollback()
                        db_pool.putconn(conn)
                    except Exception:
                        # Rollback failed: connection is severed or invalid.
                        # Discard with close=True so it never poisons the pool!
                        try:
                            db_pool.putconn(conn, close=True)
                        except Exception:
                            pass
            else:
                try:
                    conn.close()
                except Exception:
                    pass


def init_ai_summary_table() -> None:
    """Ensure the ai_summaries persistent cache table exists."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_summaries (
                    cache_key VARCHAR(64) PRIMARY KEY,
                    summary TEXT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)
            conn.commit()
        logger.info("Initialized/verified ai_summaries persistent cache table.")
    except Exception as e:
        logger.warning(f"Could not auto-create ai_summaries table: {e}")
    finally:
        if conn and not getattr(conn, "closed", 1):
            try:
                conn.close()
            except Exception:
                pass
