/* Copyright (c) 2015-2016, The Tor Project, Inc. */
/* See LICENSE for licensing information */

#include "orconfig.h"

#define CONNECTION_PRIVATE
#define MAIN_PRIVATE

#include "or.h"
#include "test.h"

#include "connection.h"
#include "main.h"
#include "networkstatus.h"
#include "rendcache.h"
#include "directory.h"

static void test_conn_lookup_addr_helper(const char *address,
                                         int family,
                                         tor_addr_t *addr);

static void * test_conn_get_basic_setup(const struct testcase_t *tc);
static int test_conn_get_basic_teardown(const struct testcase_t *tc,
                                        void *arg);

static void * test_conn_get_rend_setup(const struct testcase_t *tc);
static int test_conn_get_rend_teardown(const struct testcase_t *tc,
                                        void *arg);

/* Arbitrary choice - IPv4 Directory Connection to localhost */
#define TEST_CONN_TYPE          (CONN_TYPE_DIR)
/* We assume every machine has IPv4 localhost, is that ok? */
#define TEST_CONN_ADDRESS       "127.0.0.1"
#define TEST_CONN_PORT          (12345)
#define TEST_CONN_ADDRESS_PORT  "127.0.0.1:12345"
#define TEST_CONN_FAMILY        (AF_INET)
#define TEST_CONN_STATE         (DIR_CONN_STATE_MIN_)
#define TEST_CONN_ADDRESS_2     "127.0.0.2"

#define TEST_CONN_BASIC_PURPOSE (DIR_PURPOSE_MIN_)

#define TEST_CONN_REND_ADDR     "cfs3rltphxxvabci"
#define TEST_CONN_REND_PURPOSE  (DIR_PURPOSE_FETCH_RENDDESC_V2)
#define TEST_CONN_REND_PURPOSE_SUCCESSFUL (DIR_PURPOSE_HAS_FETCHED_RENDDESC_V2)
#define TEST_CONN_REND_TYPE_2   (CONN_TYPE_AP)
#define TEST_CONN_REND_ADDR_2   "icbavxxhptlr3sfc"

#define TEST_CONN_RSRC          (networkstatus_get_flavor_name(FLAV_MICRODESC))
#define TEST_CONN_RSRC_PURPOSE  (DIR_PURPOSE_FETCH_CONSENSUS)
#define TEST_CONN_RSRC_STATE_SUCCESSFUL (DIR_CONN_STATE_CLIENT_FINISHED)
#define TEST_CONN_RSRC_2        (networkstatus_get_flavor_name(FLAV_NS))

#define TEST_CONN_DL_STATE      (DIR_CONN_STATE_CLIENT_SENDING)

#define TEST_CONN_FD_INIT 50
static int mock_connection_connect_sockaddr_called = 0;
static int fake_socket_number = TEST_CONN_FD_INIT;

static int
mock_connection_connect_sockaddr(connection_t *conn,
                                 const struct sockaddr *sa,
                                 socklen_t sa_len,
                                 const struct sockaddr *bindaddr,
                                 socklen_t bindaddr_len,
                                 int *socket_error)
{
  (void)sa_len;
  (void)bindaddr;
  (void)bindaddr_len;

  tor_assert(conn);
  tor_assert(sa);
  tor_assert(socket_error);

  mock_connection_connect_sockaddr_called++;

  conn->s = fake_socket_number++;
  tt_assert(SOCKET_OK(conn->s));
  /* We really should call tor_libevent_initialize() here. Because we don't,
   * we are relying on other parts of the code not checking if the_event_base
   * (and therefore event->ev_base) is NULL.  */
  tt_assert(connection_add_connecting(conn) == 0);

 done:
  /* Fake "connected" status */
  return 1;
}

static void
test_conn_lookup_addr_helper(const char *address, int family, tor_addr_t *addr)
{
  int rv = 0;

  tt_assert(addr);

  rv = tor_addr_lookup(address, family, addr);
  /* XXXX - should we retry on transient failure? */
  tt_assert(rv == 0);
  tt_assert(tor_addr_is_loopback(addr));
  tt_assert(tor_addr_is_v4(addr));

  return;

 done:
  tor_addr_make_null(addr, TEST_CONN_FAMILY);
}

static void *
test_conn_get_basic_setup(const struct testcase_t *tc)
{
  connection_t *conn = NULL;
  tor_addr_t addr;
  int socket_err = 0;
  int in_progress = 0;
  (void)tc;

  MOCK(connection_connect_sockaddr,
       mock_connection_connect_sockaddr);

  init_connection_lists();

  conn = connection_new(TEST_CONN_TYPE, TEST_CONN_FAMILY);
  tt_assert(conn);

  test_conn_lookup_addr_helper(TEST_CONN_ADDRESS, TEST_CONN_FAMILY, &addr);
  tt_assert(!tor_addr_is_null(&addr));

  /* XXXX - connection_connect doesn't set these, should it? */
  tor_addr_copy_tight(&conn->addr, &addr);
  conn->port = TEST_CONN_PORT;
  mock_connection_connect_sockaddr_called = 0;
  in_progress = connection_connect(conn, TEST_CONN_ADDRESS_PORT, &addr,
                                   TEST_CONN_PORT, &socket_err);
  tt_assert(mock_connection_connect_sockaddr_called == 1);
  tt_assert(!socket_err);
  tt_assert(in_progress == 0 || in_progress == 1);

  /* fake some of the attributes so the connection looks OK */
  conn->state = TEST_CONN_STATE;
  conn->purpose = TEST_CONN_BASIC_PURPOSE;
  assert_connection_ok(conn, time(NULL));

  UNMOCK(connection_connect_sockaddr);

  return conn;

  /* On failure */
 done:
  UNMOCK(connection_connect_sockaddr);
  test_conn_get_basic_teardown(tc, conn);

  /* Returning NULL causes the unit test to fail */
  return NULL;
}

static int
test_conn_get_basic_teardown(const struct testcase_t *tc, void *arg)
{
  (void)tc;
  connection_t *conn = arg;

  tt_assert(conn);
  assert_connection_ok(conn, time(NULL));

  /* teardown the connection as fast as possible */
  if (conn->linked_conn) {
    assert_connection_ok(conn->linked_conn, time(NULL));

    /* We didn't call tor_libevent_initialize(), so event_base was NULL,
     * so we can't rely on connection_unregister_events() use of event_del().
     */
    if (conn->linked_conn->read_event) {
      tor_free(conn->linked_conn->read_event);
      conn->linked_conn->read_event = NULL;
    }
    if (conn->linked_conn->write_event) {
      tor_free(conn->linked_conn->write_event);
      conn->linked_conn->write_event = NULL;
    }

    if (!conn->linked_conn->marked_for_close) {
      connection_close_immediate(conn->linked_conn);
      connection_mark_for_close(conn->linked_conn);
    }
    conn->linked_conn->linked_conn = NULL;
    connection_free(conn->linked_conn);
    conn->linked_conn = NULL;
  }

  /* We didn't set the events up properly, so we can't use event_del() in
   * close_closeable_connections() > connection_free()
   * > connection_unregister_events() */
  if (conn->read_event) {
    tor_free(conn->read_event);
    conn->read_event = NULL;
  }
  if (conn->write_event) {
    tor_free(conn->write_event);
    conn->write_event = NULL;
  }

  if (!conn->marked_for_close) {
    connection_close_immediate(conn);
    connection_mark_for_close(conn);
  }

  close_closeable_connections();

  /* The unit test will fail if we return 0 */
  return 1;

  /* When conn == NULL, we can't cleanup anything */
 done:
  return 0;
}

static void *
test_conn_get_rend_setup(const struct testcase_t *tc)
{
  dir_connection_t *conn = DOWNCAST(dir_connection_t,
                                    test_conn_get_basic_setup(tc));
  tt_assert(conn);
  assert_connection_ok(&conn->base_, time(NULL));

  rend_cache_init();

  /* TODO: use directory_initiate_command_rend() to do this - maybe? */
  conn->rend_data = tor_malloc_zero(sizeof(rend_data_t));
  tor_assert(strlen(TEST_CONN_REND_ADDR) == REND_SERVICE_ID_LEN_BASE32);
  memcpy(conn->rend_data->onion_address,
         TEST_CONN_REND_ADDR,
         REND_SERVICE_ID_LEN_BASE32+1);
  conn->rend_data->hsdirs_fp = smartlist_new();
  conn->base_.purpose = TEST_CONN_REND_PURPOSE;

  assert_connection_ok(&conn->base_, time(NULL));
  return conn;

  /* On failure */
 done:
  test_conn_get_rend_teardown(tc, conn);
  /* Returning NULL causes the unit test to fail */
  return NULL;
}

static int
test_conn_get_rend_teardown(const struct testcase_t *tc, void *arg)
{
  dir_connection_t *conn = DOWNCAST(dir_connection_t, arg);
  int rv = 0;

  tt_assert(conn);
  assert_connection_ok(&conn->base_, time(NULL));

  /* avoid a last-ditch attempt to refetch the descriptor */
  conn->base_.purpose = TEST_CONN_REND_PURPOSE_SUCCESSFUL;

  /* connection_free_() cleans up rend_data */
  rv = test_conn_get_basic_teardown(tc, arg);
 done:
  rend_cache_free_all();
  return rv;
}

static struct testcase_setup_t test_conn_get_basic_st = {
  test_conn_get_basic_setup, test_conn_get_basic_teardown
};

static struct testcase_setup_t test_conn_get_rend_st = {
  test_conn_get_rend_setup, test_conn_get_rend_teardown
};

static void
test_conn_get_basic(void *arg)
{
  connection_t *conn = (connection_t*)arg;
  tor_addr_t addr, addr2;

  tt_assert(conn);
  assert_connection_ok(conn, time(NULL));

  test_conn_lookup_addr_helper(TEST_CONN_ADDRESS, TEST_CONN_FAMILY, &addr);
  tt_assert(!tor_addr_is_null(&addr));
  test_conn_lookup_addr_helper(TEST_CONN_ADDRESS_2, TEST_CONN_FAMILY, &addr2);
  tt_assert(!tor_addr_is_null(&addr2));

  /* Check that we get this connection back when we search for it by
   * its attributes, but get NULL when we supply a different value. */

  tt_assert(connection_get_by_global_id(conn->global_identifier) == conn);
  tt_assert(connection_get_by_global_id(!conn->global_identifier) == NULL);

  tt_assert(connection_get_by_type(conn->type) == conn);
  tt_assert(connection_get_by_type(TEST_CONN_TYPE) == conn);
  tt_assert(connection_get_by_type(!conn->type) == NULL);
  tt_assert(connection_get_by_type(!TEST_CONN_TYPE) == NULL);

  tt_assert(connection_get_by_type_state(conn->type, conn->state)
            == conn);
  tt_assert(connection_get_by_type_state(TEST_CONN_TYPE, TEST_CONN_STATE)
            == conn);
  tt_assert(connection_get_by_type_state(!conn->type, !conn->state)
            == NULL);
  tt_assert(connection_get_by_type_state(!TEST_CONN_TYPE, !TEST_CONN_STATE)
            == NULL);

  /* Match on the connection fields themselves */
  tt_assert(connection_get_by_type_addr_port_purpose(conn->type,
                                                     &conn->addr,
                                                     conn->port,
                                                     conn->purpose)
            == conn);
  /* Match on the original inputs to the connection */
  tt_assert(connection_get_by_type_addr_port_purpose(TEST_CONN_TYPE,
                                                     &conn->addr,
                                                     conn->port,
                                                     conn->purpose)
            == conn);
  tt_assert(connection_get_by_type_addr_port_purpose(conn->type,
                                                     &addr,
                                                     conn->port,
                                                     conn->purpose)
            == conn);
  tt_assert(connection_get_by_type_addr_port_purpose(conn->type,
                                                     &conn->addr,
                                                     TEST_CONN_PORT,
                                                     conn->purpose)
            == conn);
  tt_assert(connection_get_by_type_addr_port_purpose(conn->type,
                                                     &conn->addr,
                                                     conn->port,
                                                     TEST_CONN_BASIC_PURPOSE)
            == conn);
  tt_assert(connection_get_by_type_addr_port_purpose(TEST_CONN_TYPE,
                                                     &addr,
                                                     TEST_CONN_PORT,
                                                     TEST_CONN_BASIC_PURPOSE)
            == conn);
  /* Then try each of the not-matching combinations */
  tt_assert(connection_get_by_type_addr_port_purpose(!conn->type,
                                                     &conn->addr,
                                                     conn->port,
                                                     conn->purpose)
            == NULL);
  tt_assert(connection_get_by_type_addr_port_purpose(conn->type,
                                                     &addr2,
                                                     conn->port,
                                                     conn->purpose)
            == NULL);
  tt_assert(connection_get_by_type_addr_port_purpose(conn->type,
                                                     &conn->addr,
                                                     !conn->port,
                                                     conn->purpose)
            == NULL);
  tt_assert(connection_get_by_type_addr_port_purpose(conn->type,
                                                     &conn->addr,
                                                     conn->port,
                                                     !conn->purpose)
            == NULL);
  /* Then try everything not-matching */
  tt_assert(connection_get_by_type_addr_port_purpose(!conn->type,
                                                     &addr2,
                                                     !conn->port,
                                                     !conn->purpose)
            == NULL);
  tt_assert(connection_get_by_type_addr_port_purpose(!TEST_CONN_TYPE,
                                                     &addr2,
                                                     !TEST_CONN_PORT,
                                                     !TEST_CONN_BASIC_PURPOSE)
            == NULL);

 done:
  ;
}

static void
test_conn_get_rend(void *arg)
{
  dir_connection_t *conn = DOWNCAST(dir_connection_t, arg);
  tt_assert(conn);
  assert_connection_ok(&conn->base_, time(NULL));

  tt_assert(connection_get_by_type_state_rendquery(
                                            conn->base_.type,
                                            conn->base_.state,
                                            conn->rend_data->onion_address)
            == TO_CONN(conn));
  tt_assert(connection_get_by_type_state_rendquery(
                                            TEST_CONN_TYPE,
                                            TEST_CONN_STATE,
                                            TEST_CONN_REND_ADDR)
            == TO_CONN(conn));
  tt_assert(connection_get_by_type_state_rendquery(TEST_CONN_REND_TYPE_2,
                                                   !conn->base_.state,
                                                   "")
            == NULL);
  tt_assert(connection_get_by_type_state_rendquery(TEST_CONN_REND_TYPE_2,
                                                   !TEST_CONN_STATE,
                                                   TEST_CONN_REND_ADDR_2)
            == NULL);

 done:
  ;
}

#define sl_is_conn_assert(sl_input, conn) \
  do {                                               \
    the_sl = (sl_input);                             \
    tt_assert(smartlist_len((the_sl)) == 1);         \
    tt_assert(smartlist_get((the_sl), 0) == (conn)); \
    smartlist_free(the_sl); the_sl = NULL;           \
  } while (0)

#define sl_no_conn_assert(sl_input)          \
  do {                                       \
    the_sl = (sl_input);                     \
    tt_assert(smartlist_len((the_sl)) == 0); \
    smartlist_free(the_sl); the_sl = NULL;   \
  } while (0)

#define CONNECTION_TESTCASE(name, fork, setup)                           \
  { #name, test_conn_##name, fork, &setup, NULL }

struct testcase_t connection_tests[] = {
  CONNECTION_TESTCASE(get_basic, TT_FORK, test_conn_get_basic_st),
  CONNECTION_TESTCASE(get_rend,  TT_FORK, test_conn_get_rend_st),
//CONNECTION_TESTCASE(func_suffix, TT_FORK, setup_func_pair),
  END_OF_TESTCASES
};

