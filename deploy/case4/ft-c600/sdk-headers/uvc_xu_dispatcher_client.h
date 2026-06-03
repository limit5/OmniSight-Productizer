#ifndef OMNISIGHT_UVC_XU_DISPATCHER_CLIENT_H
#define OMNISIGHT_UVC_XU_DISPATCHER_CLIENT_H

#define OMNISIGHT_UVC_XU_DISPATCHER_SOCKET "/run/uvc-xu-dispatcher.sock"
#define OMNISIGHT_UVC_XU_LOOKUP_BY_GUID "LOOKUP_BY_GUID"
#define OMNISIGHT_UVC_XU_EXPECTED_FT_C600_VENDOR "ft-c600"

/*
 * Socket protocol exported by the Phase 0 userspace dispatcher:
 *
 *   LOOKUP_BY_GUID <guid-token>\n
 *
 * The FT-C600 reference bundle uses the Case 2 token carried by the
 * dispatcher smoke test: "ft-c600-xu-guid".
 */
#define OMNISIGHT_UVC_XU_FT_C600_GUID_TOKEN "ft-c600-xu-guid"

#endif /* OMNISIGHT_UVC_XU_DISPATCHER_CLIENT_H */
