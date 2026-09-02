"""
Database Base Operations

Base class for standardizing database connection patterns and error handling.
Eliminates code duplication across database operation modules.
"""

import logging
import sqlite3
from typing import Any, Callable, TypeVar
from contextlib import contextmanager
from .db_setup import create_connection

T = TypeVar('T')


class DatabaseError(Exception):
    """Custom exception for database operation errors"""
    def __init__(self, message: str, operation: str = None, context: dict = None):
        self.operation = operation
        self.context = context or {}
        super().__init__(message)


class DatabaseOperationBase:
    """
    Base class for database operations with standardized patterns

    Provides common functionality for:
    - Connection management with automatic cleanup
    - Standardized error handling and logging
    - Transaction management
    - Retry logic for common failure scenarios
    """

    def __init__(self, db_path: str, logger_name: str = None):
        self.db_path = db_path
        self.logger = logging.getLogger(logger_name or __name__)

    @contextmanager
    def get_db_connection(self, auto_commit: bool = True):
        """
        Context manager for database connections with automatic cleanup

        Args:
            auto_commit: Whether to automatically commit transactions

        Yields:
            Database connection object

        Raises:
            DatabaseError: If connection fails
        """
        conn = None
        try:
            conn = create_connection(self.db_path)
            yield conn

            if auto_commit:
                conn.commit()

        except sqlite3.Error as e:
            if conn and auto_commit:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass  # Rollback failed, but we're already handling an error

            self.logger.error(f"Database error: {e}")
            raise DatabaseError(f"Database operation failed: {e}") from e

        except Exception as e:
            if conn and auto_commit:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass

            self.logger.error(f"Unexpected error in database operation: {e}")
            raise DatabaseError(f"Unexpected database error: {e}") from e

        finally:
            if conn:
                try:
                    conn.close()
                except sqlite3.Error as e:
                    self.logger.warning(f"Error closing database connection: {e}")

    def execute_query(self,
                     query: str,
                     params: tuple = None,
                     fetch_one: bool = False,
                     fetch_all: bool = True,
                     operation_name: str = None) -> Any:
        """
        Execute a database query with standardized error handling

        Args:
            query: SQL query string
            params: Query parameters tuple
            fetch_one: Return single row result
            fetch_all: Return all rows (ignored if fetch_one=True)
            operation_name: Description of operation for logging

        Returns:
            Query results or None

        Raises:
            DatabaseError: If query execution fails
        """
        operation = operation_name or "query execution"

        try:
            with self.get_db_connection() as conn:
                cursor = conn.cursor()

                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)

                if fetch_one:
                    result = cursor.fetchone()
                    return result
                elif fetch_all:
                    result = cursor.fetchall()
                    return result
                else:
                    # For INSERT, UPDATE, DELETE operations
                    rowcount = cursor.rowcount
                    return rowcount

        except DatabaseError:
            raise  # Re-raise our custom errors
        except Exception as e:
            error_msg = f"Failed to execute {operation}: {e}"
            self.logger.error(error_msg)
            raise DatabaseError(error_msg, operation, {'query': query, 'params': params}) from e

    def execute_transaction(self, operations: list[Callable], operation_name: str = None) -> bool:
        """
        Execute multiple operations in a single transaction

        Args:
            operations: List of callable operations to execute
            operation_name: Description for logging

        Returns:
            True if all operations succeeded

        Raises:
            DatabaseError: If any operation fails
        """
        operation = operation_name or "transaction"

        try:
            with self.get_db_connection(auto_commit=False) as conn:
                try:
                    for i, op in enumerate(operations):
                        op(conn)

                    conn.commit()
                    return True

                except Exception as e:
                    conn.rollback()
                    raise e

        except Exception as e:
            error_msg = f"Transaction failed for {operation}: {e}"
            self.logger.error(error_msg)
            raise DatabaseError(error_msg, operation) from e

    def check_record_exists(self, table: str, where_clause: str, params: tuple = None) -> bool:
        """
        Check if a record exists in the database

        Args:
            table: Table name
            where_clause: WHERE clause (without WHERE keyword)
            params: Parameters for WHERE clause

        Returns:
            True if record exists, False otherwise
        """
        query = f"SELECT 1 FROM {table} WHERE {where_clause} LIMIT 1"
        result = self.execute_query(
            query,
            params,
            fetch_one=True,
            operation_name=f"check existence in {table}"
        )
        return result is not None

    def get_record_count(self, table: str, where_clause: str = None, params: tuple = None) -> int:
        """
        Get count of records matching criteria

        Args:
            table: Table name
            where_clause: Optional WHERE clause (without WHERE keyword)
            params: Parameters for WHERE clause

        Returns:
            Number of matching records
        """
        if where_clause:
            query = f"SELECT COUNT(*) FROM {table} WHERE {where_clause}"
        else:
            query = f"SELECT COUNT(*) FROM {table}"

        result = self.execute_query(
            query,
            params,
            fetch_one=True,
            operation_name=f"count records in {table}"
        )
        return result[0] if result else 0

    def insert_record(self, table: str, data: dict, operation_name: str = None) -> int:
        """
        Insert a record into the database

        Args:
            table: Table name
            data: Dictionary of column -> value mappings
            operation_name: Description for logging

        Returns:
            ID of inserted record
        """
        if not data:
            raise ValueError("Data dictionary cannot be empty")

        columns = list(data.keys())
        placeholders = ', '.join(['?' for _ in columns])
        values = tuple(data.values())

        query = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"

        operation = operation_name or f"insert into {table}"

        try:
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, values)
                record_id = cursor.lastrowid

                self.logger.debug(f"Record inserted successfully for {operation}")
                return record_id

        except Exception as e:
            error_msg = f"Failed to {operation}: {e}"
            self.logger.error(error_msg)
            raise DatabaseError(error_msg, operation, {'table': table, 'data': data}) from e

    def update_record(self, table: str, data: dict, where_clause: str, where_params: tuple = None, operation_name: str = None) -> int:
        """
        Update records in the database

        Args:
            table: Table name
            data: Dictionary of column -> value mappings to update
            where_clause: WHERE clause (without WHERE keyword)
            where_params: Parameters for WHERE clause
            operation_name: Description for logging

        Returns:
            Number of affected rows
        """
        if not data:
            raise ValueError("Data dictionary cannot be empty")

        set_clause = ', '.join([f"{col} = ?" for col in data.keys()])
        values = list(data.values())

        if where_params:
            values.extend(where_params)

        query = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"

        return self.execute_query(
            query,
            tuple(values),
            fetch_all=False,
            operation_name=operation_name or f"update {table}"
        )

    def delete_record(self, table: str, where_clause: str, where_params: tuple = None, operation_name: str = None) -> int:
        """
        Delete records from the database

        Args:
            table: Table name
            where_clause: WHERE clause (without WHERE keyword)
            where_params: Parameters for WHERE clause
            operation_name: Description for logging

        Returns:
            Number of deleted rows
        """
        query = f"DELETE FROM {table} WHERE {where_clause}"

        return self.execute_query(
            query,
            where_params,
            fetch_all=False,
            operation_name=operation_name or f"delete from {table}"
        )