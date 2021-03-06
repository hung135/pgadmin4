##########################################################################
#
# pgAdmin 4 - PostgreSQL Tools
#
# Copyright (C) 2013 - 2018, The pgAdmin Development Team
# This software is released under the PostgreSQL Licence
#
##########################################################################

"""Start executing the query in async mode."""

import pickle
import random

from flask import Response
from flask_babel import gettext

from config import PG_DEFAULT_DRIVER
from pgadmin.tools.sqleditor.utils.apply_explain_plan_wrapper import apply_explain_plan_wrapper_if_needed
from pgadmin.tools.sqleditor.utils.update_session_grid_transaction import update_session_grid_transaction
from pgadmin.tools.sqleditor.utils.is_begin_required import is_begin_required
from pgadmin.tools.sqleditor.utils.constant_definition import TX_STATUS_IDLE, TX_STATUS_INERROR

from pgadmin.utils.ajax import make_json_response, internal_server_error
from pgadmin.utils.driver import get_driver
from pgadmin.utils.exception import ConnectionLost


class StartRunningQuery:

    def __init__(self, blueprint_object, logger):
        self.http_session = None
        self.blueprint_object = blueprint_object
        self.connection_id = str(random.randint(1, 9999999))
        self.logger = logger

    def execute(self, sql, trans_id, http_session):
        session_obj = StartRunningQuery.retrieve_session_information(http_session, trans_id)
        if type(session_obj) is Response:
            return session_obj

        transaction_object = pickle.loads(session_obj['command_obj'])
        can_edit = False
        can_filter = False
        if transaction_object is not None and session_obj is not None:
            # set fetched row count to 0 as we are executing query again.
            transaction_object.update_fetched_row_cnt(0)
            self.__retrieve_connection_id(transaction_object)

            try:
                manager = get_driver(
                    PG_DEFAULT_DRIVER).connection_manager(transaction_object.sid)
                conn = manager.connection(did=transaction_object.did, conn_id=self.connection_id,
                                          auto_reconnect=False,
                                          use_binary_placeholder=True,
                                          array_to_string=True)
            except ConnectionLost:
                raise
            except Exception as e:
                self.logger.error(e)
                return internal_server_error(errormsg=str(e))

            # Connect to the Server if not connected.
            if not conn.connected():
                status, msg = conn.connect()
                if not status:
                    self.logger.error(msg)
                    return internal_server_error(errormsg=str(msg))

            effective_sql_statement = apply_explain_plan_wrapper_if_needed(manager, sql)

            result, status = self.__execute_query(
                conn,
                session_obj,
                effective_sql_statement,
                trans_id,
                transaction_object
            )

            can_edit = transaction_object.can_edit()
            can_filter = transaction_object.can_filter()

        else:
            status = False
            result = gettext(
                'Either transaction object or session object not found.')
        return make_json_response(
            data={
                'status': status, 'result': result,
                'can_edit': can_edit, 'can_filter': can_filter,
                'info_notifier_timeout': self.blueprint_object.info_notifier_timeout.get()
            }
        )

    def __retrieve_connection_id(self, trans_obj):
        conn_id = trans_obj.conn_id
        # if conn_id is None then we will have to create a new connection
        if conn_id is not None:
            self.connection_id = conn_id

    def __execute_query(self, conn, session_obj, sql, trans_id, trans_obj):
        if conn.connected():
            # on successful connection set the connection id to the
            # transaction object
            trans_obj.set_connection_id(self.connection_id)

            StartRunningQuery.save_transaction_in_session(session_obj, trans_id, trans_obj)

            # If auto commit is False and transaction status is Idle
            # then call is_begin_not_required() function to check BEGIN
            # is required or not.

            if StartRunningQuery.is_begin_required_for_sql_query(trans_obj, conn, sql):
                conn.execute_void("BEGIN;")

            # Execute sql asynchronously with params is None
            # and formatted_error is True.
            try:
                status, result = conn.execute_async(sql)
            except ConnectionLost:
                raise

            # If the transaction aborted for some reason and
            # Auto RollBack is True then issue a rollback to cleanup.
            if StartRunningQuery.is_rollback_statement_required(trans_obj, conn):
                conn.execute_void("ROLLBACK;")
        else:
            status = False
            result = gettext(
                'Not connected to server or connection with the server has '
                'been closed.')
        return result, status

    @staticmethod
    def is_begin_required_for_sql_query(trans_obj, conn, sql):
        return not trans_obj.auto_commit \
               and conn.transaction_status() == TX_STATUS_IDLE \
               and is_begin_required(sql)

    @staticmethod
    def is_rollback_statement_required(trans_obj, conn):
        return conn.transaction_status() == TX_STATUS_INERROR and trans_obj.auto_rollback

    @staticmethod
    def save_transaction_in_session(session, transaction_id, transaction):
        # As we changed the transaction object we need to
        # restore it and update the session variable.
        session['command_obj'] = pickle.dumps(transaction, -1)
        update_session_grid_transaction(transaction_id, session)

    @staticmethod
    def retrieve_session_information(http_session, transaction_id):
        if 'gridData' not in http_session:
            return make_json_response(
                success=0,
                errormsg=gettext('Transaction ID not found in the session.'),
                info='DATAGRID_TRANSACTION_REQUIRED', status=404
            )
        grid_data = http_session['gridData']
        # Return from the function if transaction id not found
        if str(transaction_id) not in grid_data:
            return make_json_response(
                success=0,
                errormsg=gettext('Transaction ID not found in the session.'),
                info='DATAGRID_TRANSACTION_REQUIRED',
                status=404
            )
        # Fetch the object for the specified transaction id.
        # Use pickle.loads function to get the command object
        return grid_data[str(transaction_id)]


