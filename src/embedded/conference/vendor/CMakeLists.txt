# Case 5 conference vendor SDK CMake fragment (OP-2005, OP-2007, OP-2008).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.
# Vendor Linux SDKs are fetched from the NDA mirror during builds; the static
# link path bridges each SDK's glibc 2.31 ABI on RK3588 aarch64. If an SDK's
# runtime invariants reject static linking, package-level container fallback
# remains outside these adapters.

# ── Zoom Linux Meeting SDK adapter (OP-2005) ────────────────────────

add_library(omnisight-conference-zoom-adapter STATIC
  ${CMAKE_CURRENT_LIST_DIR}/zoom-adapter.cpp
)

target_include_directories(omnisight-conference-zoom-adapter PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-conference-zoom-adapter PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-conference-zoom-adapter PRIVATE
  -Wall
  -Wextra
  -Werror
)

set(OMNISIGHT_ZOOM_SDK_ROOT "" CACHE PATH
  "NDA-mirror Zoom Linux Meeting SDK root for static-link builds")

if(OMNISIGHT_ZOOM_SDK_ROOT)
  find_library(OMNISIGHT_ZOOMSDK_LIBRARY
    NAMES libzoomsdk.a
    PATHS ${OMNISIGHT_ZOOM_SDK_ROOT}/lib
    NO_DEFAULT_PATH
    REQUIRED
  )

  target_compile_definitions(omnisight-conference-zoom-adapter PRIVATE
    OMNISIGHT_ZOOM_WITH_LINUX_SDK=1
  )
  target_include_directories(omnisight-conference-zoom-adapter PRIVATE
    ${OMNISIGHT_ZOOM_SDK_ROOT}/include
  )
  target_link_directories(omnisight-conference-zoom-adapter PUBLIC
    ${OMNISIGHT_ZOOM_SDK_ROOT}/lib
  )
  target_link_libraries(omnisight-conference-zoom-adapter PUBLIC
    ${OMNISIGHT_ZOOMSDK_LIBRARY}
  )
endif()

# ── Microsoft ACS Calling SDK adapter (OP-2007; Teams-interop) ──────

add_library(omnisight-conference-vendor-acs STATIC
  ${CMAKE_CURRENT_LIST_DIR}/acs-adapter.cpp
)

target_include_directories(omnisight-conference-vendor-acs PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-conference-vendor-acs PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-conference-vendor-acs PRIVATE
  -Wall
  -Wextra
  -Werror
)

find_path(ACS_CALLING_SDK_INCLUDE_DIR
  NAMES omnisight_acs_calling_sdk.h acs_calling_sdk.h
)
find_library(ACS_CALLING_SDK_LIBRARY
  NAMES acs_calling_sdk acs-calling-sdk
)

if(ACS_CALLING_SDK_INCLUDE_DIR AND ACS_CALLING_SDK_LIBRARY)
  target_compile_definitions(omnisight-conference-vendor-acs PRIVATE
    OMNISIGHT_ACS_WITH_SDK=1
  )
  target_include_directories(omnisight-conference-vendor-acs PRIVATE
    ${ACS_CALLING_SDK_INCLUDE_DIR}
  )
  target_link_libraries(omnisight-conference-vendor-acs PUBLIC
    ${ACS_CALLING_SDK_LIBRARY}
  )
endif()

add_executable(omnisight-conference-vendor-acs-smoke
  ${CMAKE_CURRENT_LIST_DIR}/acs-adapter.cpp
)
target_compile_definitions(omnisight-conference-vendor-acs-smoke PRIVATE
  OMNISIGHT_ACS_ADAPTER_SMOKE_MAIN=1
)
target_compile_features(omnisight-conference-vendor-acs-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-conference-vendor-acs-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
target_include_directories(omnisight-conference-vendor-acs-smoke PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
)
if(ACS_CALLING_SDK_INCLUDE_DIR AND ACS_CALLING_SDK_LIBRARY)
  target_compile_definitions(omnisight-conference-vendor-acs-smoke PRIVATE
    OMNISIGHT_ACS_WITH_SDK=1
  )
  target_include_directories(omnisight-conference-vendor-acs-smoke PRIVATE
    ${ACS_CALLING_SDK_INCLUDE_DIR}
  )
  target_link_libraries(omnisight-conference-vendor-acs-smoke PRIVATE
    ${ACS_CALLING_SDK_LIBRARY}
  )
endif()

# ── Cisco Webex Calling SDK adapter (OP-2008) ───────────────────────

add_library(omnisight-conference-webex-adapter STATIC
  ${CMAKE_CURRENT_LIST_DIR}/webex-adapter.cpp
)

target_include_directories(omnisight-conference-webex-adapter PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-conference-webex-adapter PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-conference-webex-adapter PRIVATE
  -Wall
  -Wextra
  -Werror
)

set(OMNISIGHT_WEBEX_SDK_ROOT "" CACHE PATH
  "NDA-mirror Cisco Webex Calling SDK root for static-link builds")

if(OMNISIGHT_WEBEX_SDK_ROOT)
  find_library(OMNISIGHT_WEBEXSDK_LIBRARY
    NAMES libwebexcallingsdk.a
    PATHS ${OMNISIGHT_WEBEX_SDK_ROOT}/lib
    NO_DEFAULT_PATH
    REQUIRED
  )

  target_compile_definitions(omnisight-conference-webex-adapter PRIVATE
    OMNISIGHT_WEBEX_WITH_CALLING_SDK=1
  )
  target_include_directories(omnisight-conference-webex-adapter PRIVATE
    ${OMNISIGHT_WEBEX_SDK_ROOT}/include
  )
  target_link_directories(omnisight-conference-webex-adapter PUBLIC
    ${OMNISIGHT_WEBEX_SDK_ROOT}/lib
  )
  target_link_libraries(omnisight-conference-webex-adapter PUBLIC
    ${OMNISIGHT_WEBEXSDK_LIBRARY}
  )
endif()

add_executable(omnisight-conference-webex-adapter-smoke
  ${CMAKE_CURRENT_LIST_DIR}/webex-adapter.cpp
)
target_compile_definitions(omnisight-conference-webex-adapter-smoke PRIVATE
  OMNISIGHT_WEBEX_ADAPTER_SMOKE_MAIN=1
)
target_compile_features(omnisight-conference-webex-adapter-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-conference-webex-adapter-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
