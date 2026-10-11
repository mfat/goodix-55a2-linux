/*
 * libfprint TOD driver for Goodix 27c6:55a2 (bridge client).
 *
 * The sensor itself is driven by goodix55a2-bridge (USB + TLS + matching).
 * For every enroll/verify/identify this driver opens a fresh connection to the
 * bridge socket and sends one request line; the bridge only answers with touches
 * that happen after the request, so nothing can be queued or replayed.
 *
 * Bridge replies (one per line):
 *   FINGER <0|1>   finger status change
 *   STAGE <n>      enroll stage n completed
 *   RETRY <code>   FpDeviceRetry code, the current stage/attempt must be redone
 *   DONE <id>      enroll finished, template id
 *   MATCH [<id>]   verify / identify succeeded
 *   NOMATCH        verify / identify failed
 *   ERROR <msg>    fatal error
 */

#define FP_COMPONENT "goodix55a2"

#include "drivers_api.h"

#define BRIDGE_SOCKET "/run/goodix55a2/bridge.sock"
#define ENROLL_STAGES 10

struct _FpiDeviceGoodix55a2
{
  FpDevice           parent;

  GSocketConnection *conn;
  GDataInputStream  *in;
  gint               stage;
};

G_DECLARE_FINAL_TYPE (FpiDeviceGoodix55a2, fpi_device_goodix55a2, FPI, DEVICE_GOODIX55A2, FpDevice)
G_DEFINE_TYPE (FpiDeviceGoodix55a2, fpi_device_goodix55a2, FP_TYPE_DEVICE)

static const FpIdEntry id_table[] = {
  { .vid = 0x27c6, .pid = 0x55a2, },
  { .vid = 0, .pid = 0, .driver_data = 0 },
};

static void read_next_line (FpiDeviceGoodix55a2 *self);

static void
bridge_disconnect (FpiDeviceGoodix55a2 *self)
{
  g_clear_object (&self->in);
  if (self->conn)
    g_io_stream_close (G_IO_STREAM (self->conn), NULL, NULL);
  g_clear_object (&self->conn);
}

static gchar *
print_get_id (FpPrint *print)
{
  g_autoptr(GVariant) data = NULL;

  g_object_get (print, "fpi-data", &data, NULL);
  if (!data || !g_variant_is_of_type (data, G_VARIANT_TYPE_STRING))
    return NULL;
  return g_variant_dup_string (data, NULL);
}

/* Finish the current action with @error (may be NULL for verify/identify). */
static void
action_finish (FpiDeviceGoodix55a2 *self, GError *error)
{
  FpDevice *dev = FP_DEVICE (self);

  bridge_disconnect (self);
  fpi_device_report_finger_status_changes (dev, FP_FINGER_STATUS_NONE,
                                           FP_FINGER_STATUS_PRESENT | FP_FINGER_STATUS_NEEDED);

  switch (fpi_device_get_current_action (dev))
    {
    case FPI_DEVICE_ACTION_ENROLL:
      fpi_device_enroll_complete (dev, NULL, error);
      break;

    case FPI_DEVICE_ACTION_VERIFY:
      fpi_device_verify_complete (dev, error);
      break;

    case FPI_DEVICE_ACTION_IDENTIFY:
      fpi_device_identify_complete (dev, error);
      break;

    default:
      if (error)
        fpi_device_action_error (dev, error);
    }
}

static void
handle_retry (FpiDeviceGoodix55a2 *self, FpDeviceRetry code)
{
  FpDevice *dev = FP_DEVICE (self);

  switch (fpi_device_get_current_action (dev))
    {
    case FPI_DEVICE_ACTION_ENROLL:
      fpi_device_enroll_progress (dev, self->stage, NULL, fpi_device_retry_new (code));
      read_next_line (self);
      break;

    case FPI_DEVICE_ACTION_VERIFY:
      fpi_device_verify_report (dev, FPI_MATCH_ERROR, NULL, fpi_device_retry_new (code));
      action_finish (self, NULL);
      break;

    case FPI_DEVICE_ACTION_IDENTIFY:
      fpi_device_identify_report (dev, NULL, NULL, fpi_device_retry_new (code));
      action_finish (self, NULL);
      break;

    default:
      action_finish (self, fpi_device_error_new (FP_DEVICE_ERROR_PROTO));
    }
}

static void
handle_match (FpiDeviceGoodix55a2 *self, const gchar *id)
{
  FpDevice *dev = FP_DEVICE (self);

  if (fpi_device_get_current_action (dev) == FPI_DEVICE_ACTION_VERIFY)
    {
      fpi_device_verify_report (dev, id ? FPI_MATCH_SUCCESS : FPI_MATCH_FAIL, NULL, NULL);
    }
  else if (fpi_device_get_current_action (dev) == FPI_DEVICE_ACTION_IDENTIFY)
    {
      GPtrArray *prints;
      FpPrint *match = NULL;

      fpi_device_get_identify_data (dev, &prints);
      for (guint i = 0; id && i < prints->len; i++)
        {
          g_autofree gchar *pid = print_get_id (g_ptr_array_index (prints, i));

          if (g_strcmp0 (pid, id) == 0)
            {
              match = g_ptr_array_index (prints, i);
              break;
            }
        }
      fpi_device_identify_report (dev, match, NULL, NULL);
    }
  else
    {
      action_finish (self, fpi_device_error_new (FP_DEVICE_ERROR_PROTO));
      return;
    }

  action_finish (self, NULL);
}

static void
handle_line (FpiDeviceGoodix55a2 *self, const gchar *line)
{
  FpDevice *dev = FP_DEVICE (self);

  fp_dbg ("bridge: %s", line);

  if (g_str_has_prefix (line, "FINGER "))
    {
      if (atoi (line + 7))
        fpi_device_report_finger_status_changes (dev, FP_FINGER_STATUS_PRESENT, FP_FINGER_STATUS_NEEDED);
      else
        fpi_device_report_finger_status_changes (dev, FP_FINGER_STATUS_NEEDED, FP_FINGER_STATUS_PRESENT);
      read_next_line (self);
    }
  else if (g_str_has_prefix (line, "STAGE ") &&
           fpi_device_get_current_action (dev) == FPI_DEVICE_ACTION_ENROLL)
    {
      FpPrint *print;

      fpi_device_get_enroll_data (dev, &print);
      self->stage = atoi (line + 6);
      fpi_device_enroll_progress (dev, self->stage, print, NULL);
      read_next_line (self);
    }
  else if (g_str_has_prefix (line, "DONE ") &&
           fpi_device_get_current_action (dev) == FPI_DEVICE_ACTION_ENROLL)
    {
      FpPrint *print;

      fpi_device_get_enroll_data (dev, &print);
      fpi_print_set_type (print, FPI_PRINT_RAW);
      g_object_set (print, "fpi-data", g_variant_new_string (line + 5), NULL);
      bridge_disconnect (self);
      fpi_device_report_finger_status_changes (dev, FP_FINGER_STATUS_NONE,
                                               FP_FINGER_STATUS_PRESENT | FP_FINGER_STATUS_NEEDED);
      fpi_device_enroll_complete (dev, g_object_ref (print), NULL);
    }
  else if (g_str_has_prefix (line, "RETRY "))
    {
      handle_retry (self, atoi (line + 6));
    }
  else if (g_str_equal (line, "MATCH") || g_str_has_prefix (line, "MATCH "))
    {
      handle_match (self, line[5] == ' ' ? line + 6 : "");
    }
  else if (g_str_equal (line, "NOMATCH"))
    {
      handle_match (self, NULL);
    }
  else
    {
      action_finish (self, fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL,
                                                     "bridge: %s", line));
    }
}

static void
read_line_cb (GObject *source, GAsyncResult *res, gpointer user_data)
{
  g_autoptr(GError) error = NULL;
  g_autofree gchar *line = NULL;
  FpiDeviceGoodix55a2 *self;

  line = g_data_input_stream_read_line_finish (G_DATA_INPUT_STREAM (source), res, NULL, &error);
  if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
    {
      /* The device may already be gone; the cancellable belongs to the action. */
      self = FPI_DEVICE_GOODIX55A2 (user_data);
      action_finish (self, g_steal_pointer (&error));
      return;
    }

  self = FPI_DEVICE_GOODIX55A2 (user_data);
  if (error || !line)
    {
      action_finish (self, fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL,
                                                     "bridge connection lost: %s",
                                                     error ? error->message : "EOF"));
      return;
    }

  handle_line (self, line);
}

static void
read_next_line (FpiDeviceGoodix55a2 *self)
{
  g_data_input_stream_read_line_async (self->in, G_PRIORITY_DEFAULT,
                                       fpi_device_get_cancellable (FP_DEVICE (self)),
                                       read_line_cb, self);
}

static void
bridge_request (FpiDeviceGoodix55a2 *self, const gchar *request)
{
  g_autoptr(GError) error = NULL;
  g_autoptr(GSocketClient) client = g_socket_client_new ();
  const gchar *path = g_getenv ("GOODIX55A2_BRIDGE_SOCKET");   /* testing only */
  g_autoptr(GSocketAddress) addr = g_unix_socket_address_new (path ? path : BRIDGE_SOCKET);
  g_autofree gchar *msg = g_strconcat (request, "\n", NULL);

  bridge_disconnect (self);
  self->stage = 0;
  self->conn = g_socket_client_connect (client, G_SOCKET_CONNECTABLE (addr), NULL, &error);
  if (!self->conn ||
      !g_output_stream_write_all (g_io_stream_get_output_stream (G_IO_STREAM (self->conn)),
                                  msg, strlen (msg), NULL, NULL, &error))
    {
      action_finish (self, fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL,
                                                     "goodix55a2-bridge unavailable: %s",
                                                     error->message));
      return;
    }

  self->in = g_data_input_stream_new (g_io_stream_get_input_stream (G_IO_STREAM (self->conn)));
  fpi_device_report_finger_status_changes (FP_DEVICE (self), FP_FINGER_STATUS_NEEDED,
                                           FP_FINGER_STATUS_NONE);
  read_next_line (self);
}

static void
dev_open (FpDevice *dev)
{
  fpi_device_open_complete (dev, NULL);
}

static void
dev_close (FpDevice *dev)
{
  bridge_disconnect (FPI_DEVICE_GOODIX55A2 (dev));
  fpi_device_close_complete (dev, NULL);
}

static void
dev_enroll (FpDevice *dev)
{
  g_autofree gchar *req = g_strdup_printf ("ENROLL %d", fp_device_get_nr_enroll_stages (dev));

  bridge_request (FPI_DEVICE_GOODIX55A2 (dev), req);
}

static void
dev_verify (FpDevice *dev)
{
  FpPrint *print;
  g_autofree gchar *id = NULL;
  g_autofree gchar *req = NULL;

  fpi_device_get_verify_data (dev, &print);
  id = print_get_id (print);
  if (!id)
    {
      fpi_device_verify_complete (dev, fpi_device_error_new (FP_DEVICE_ERROR_DATA_INVALID));
      return;
    }
  req = g_strconcat ("VERIFY ", id, NULL);
  bridge_request (FPI_DEVICE_GOODIX55A2 (dev), req);
}

static void
dev_identify (FpDevice *dev)
{
  GPtrArray *prints;
  g_autoptr(GString) req = g_string_new ("IDENTIFY");

  fpi_device_get_identify_data (dev, &prints);
  for (guint i = 0; i < prints->len; i++)
    {
      g_autofree gchar *id = print_get_id (g_ptr_array_index (prints, i));

      if (id)
        g_string_append_printf (req, " %s", id);
    }
  bridge_request (FPI_DEVICE_GOODIX55A2 (dev), req->str);
}

static void
dev_cancel (FpDevice *dev)
{
  /* The pending read is cancelled through the action cancellable; closing the
   * connection also tells the bridge to stop waiting for a touch. */
}

static void
fpi_device_goodix55a2_init (FpiDeviceGoodix55a2 *self)
{
}

static void
fpi_device_goodix55a2_finalize (GObject *object)
{
  bridge_disconnect (FPI_DEVICE_GOODIX55A2 (object));
  G_OBJECT_CLASS (fpi_device_goodix55a2_parent_class)->finalize (object);
}

static void
fpi_device_goodix55a2_class_init (FpiDeviceGoodix55a2Class *klass)
{
  FpDeviceClass *dev_class = FP_DEVICE_CLASS (klass);

  G_OBJECT_CLASS (klass)->finalize = fpi_device_goodix55a2_finalize;

  dev_class->id = "goodix55a2";
  dev_class->full_name = "Goodix 55a2 Fingerprint Sensor (bridge)";
  dev_class->type = FP_DEVICE_TYPE_USB;
  dev_class->id_table = id_table;
  dev_class->nr_enroll_stages = ENROLL_STAGES;
  dev_class->scan_type = FP_SCAN_TYPE_PRESS;
  dev_class->temp_hot_seconds = -1;

  dev_class->open = dev_open;
  dev_class->close = dev_close;
  dev_class->enroll = dev_enroll;
  dev_class->verify = dev_verify;
  dev_class->identify = dev_identify;
  dev_class->cancel = dev_cancel;

  fpi_device_class_auto_initialize_features (dev_class);
}

/* Entry point looked up by libfprint-tod when loading the module. */
GType
fpi_tod_shared_driver_get_type (void)
{
  return fpi_device_goodix55a2_get_type ();
}
