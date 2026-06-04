# Case 7 POS HSM clients CMake fragment (OP-2027 Thales, OP-2036 Utimaco,
# OP-2043 SafeNet Luna).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.
# Vendor HSM SDKs are fetched from the NDA mirror (OP-491-style infra). The
# static-link path bridges each SDK's glibc ABI; container fallback remains
# outside this wrapper when vendor runtime invariants reject static linking.

# ── Unified HSM client registry (OP-2050) ───────────────────────────

add_library(omnisight-pos-hsm-client-registry STATIC
  ${CMAKE_CURRENT_LIST_DIR}/hsm-client-registry.cpp
)
target_include_directories(omnisight-pos-hsm-client-registry PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-pos-hsm-client-registry PUBLIC cxx_std_17)
target_compile_options(omnisight-pos-hsm-client-registry PRIVATE -Wall -Wextra -Werror)

# ── Thales nShield HSM client (OP-2027) ─────────────────────────────

add_library(omnisight-pos-hsm-thales-nshield-client STATIC
  ${CMAKE_CURRENT_LIST_DIR}/thales-nshield-client.cpp
)
target_include_directories(omnisight-pos-hsm-thales-nshield-client PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-pos-hsm-thales-nshield-client PUBLIC cxx_std_17)
target_compile_options(omnisight-pos-hsm-thales-nshield-client PRIVATE -Wall -Wextra -Werror)
target_link_libraries(omnisight-pos-hsm-thales-nshield-client PUBLIC
  omnisight-pos-hsm-client-registry
)

set(OMNISIGHT_THALES_NSHIELD_SDK_ROOT "" CACHE PATH
  "NDA-mirror Thales nShield SDK root for static-link builds")

if(OMNISIGHT_THALES_NSHIELD_SDK_ROOT)
  find_library(OMNISIGHT_THALES_NSHIELD_LIBRARY
    NAMES libnfast.a
    PATHS ${OMNISIGHT_THALES_NSHIELD_SDK_ROOT}/lib
    NO_DEFAULT_PATH
    REQUIRED
  )
  target_compile_definitions(omnisight-pos-hsm-thales-nshield-client PRIVATE
    OMNISIGHT_HSM_WITH_THALES_NSHIELD=1
  )
  target_include_directories(omnisight-pos-hsm-thales-nshield-client PRIVATE
    ${OMNISIGHT_THALES_NSHIELD_SDK_ROOT}/include
  )
  target_link_libraries(omnisight-pos-hsm-thales-nshield-client PUBLIC
    ${OMNISIGHT_THALES_NSHIELD_LIBRARY}
  )
endif()

add_executable(omnisight-pos-hsm-thales-nshield-client-smoke
  ${CMAKE_CURRENT_LIST_DIR}/thales-nshield-client.cpp
)
target_compile_definitions(omnisight-pos-hsm-thales-nshield-client-smoke PRIVATE
  OMNISIGHT_THALES_NSHIELD_CLIENT_SMOKE_MAIN=1
)
target_compile_features(omnisight-pos-hsm-thales-nshield-client-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-pos-hsm-thales-nshield-client-smoke PRIVATE -Wall -Wextra -Werror)
target_link_libraries(omnisight-pos-hsm-thales-nshield-client-smoke PRIVATE
  omnisight-pos-hsm-client-registry
)

# ── Utimaco SecurityServer HSM client (OP-2036) ─────────────────────

add_library(omnisight-pos-hsm-utimaco-client STATIC
  ${CMAKE_CURRENT_LIST_DIR}/utimaco-client.cpp
)
target_include_directories(omnisight-pos-hsm-utimaco-client PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-pos-hsm-utimaco-client PUBLIC cxx_std_17)
target_compile_options(omnisight-pos-hsm-utimaco-client PRIVATE -Wall -Wextra -Werror)
target_link_libraries(omnisight-pos-hsm-utimaco-client PUBLIC
  omnisight-pos-hsm-client-registry
)

set(OMNISIGHT_UTIMACO_SDK_ROOT "" CACHE PATH
  "NDA-mirror Utimaco SecurityServer SDK root for static-link builds")

if(OMNISIGHT_UTIMACO_SDK_ROOT)
  find_library(OMNISIGHT_UTIMACO_LIBRARY
    NAMES libcxi.a libcs2.a
    PATHS ${OMNISIGHT_UTIMACO_SDK_ROOT}/lib
    NO_DEFAULT_PATH
    REQUIRED
  )
  target_compile_definitions(omnisight-pos-hsm-utimaco-client PRIVATE
    OMNISIGHT_HSM_WITH_UTIMACO=1
  )
  target_include_directories(omnisight-pos-hsm-utimaco-client PRIVATE
    ${OMNISIGHT_UTIMACO_SDK_ROOT}/include
  )
  target_link_libraries(omnisight-pos-hsm-utimaco-client PUBLIC
    ${OMNISIGHT_UTIMACO_LIBRARY}
  )
endif()

add_executable(omnisight-pos-hsm-utimaco-client-smoke
  ${CMAKE_CURRENT_LIST_DIR}/utimaco-client.cpp
)
target_compile_definitions(omnisight-pos-hsm-utimaco-client-smoke PRIVATE
  OMNISIGHT_UTIMACO_CLIENT_SMOKE_MAIN=1
)
target_compile_features(omnisight-pos-hsm-utimaco-client-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-pos-hsm-utimaco-client-smoke PRIVATE -Wall -Wextra -Werror)
target_link_libraries(omnisight-pos-hsm-utimaco-client-smoke PRIVATE
  omnisight-pos-hsm-client-registry
)

# ── SafeNet Luna Network HSM client (OP-2043) ───────────────────────

add_library(omnisight-pos-hsm-safenet-luna-client STATIC
  ${CMAKE_CURRENT_LIST_DIR}/safenet-luna-client.cpp
)
target_include_directories(omnisight-pos-hsm-safenet-luna-client PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-pos-hsm-safenet-luna-client PUBLIC cxx_std_17)
target_compile_options(omnisight-pos-hsm-safenet-luna-client PRIVATE -Wall -Wextra -Werror)
target_link_libraries(omnisight-pos-hsm-safenet-luna-client PUBLIC
  omnisight-pos-hsm-client-registry
)

set(OMNISIGHT_SAFENET_LUNA_SDK_ROOT "" CACHE PATH
  "NDA-mirror SafeNet Luna SDK root for static-link builds")

if(OMNISIGHT_SAFENET_LUNA_SDK_ROOT)
  find_library(OMNISIGHT_SAFENET_LUNA_LIBRARY
    NAMES libCryptoki2_64.a libCryptoki2.a
    PATHS ${OMNISIGHT_SAFENET_LUNA_SDK_ROOT}/lib
    NO_DEFAULT_PATH
    REQUIRED
  )
  target_compile_definitions(omnisight-pos-hsm-safenet-luna-client PRIVATE
    OMNISIGHT_HSM_WITH_SAFENET_LUNA=1
  )
  target_include_directories(omnisight-pos-hsm-safenet-luna-client PRIVATE
    ${OMNISIGHT_SAFENET_LUNA_SDK_ROOT}/include
  )
  target_link_libraries(omnisight-pos-hsm-safenet-luna-client PUBLIC
    ${OMNISIGHT_SAFENET_LUNA_LIBRARY}
  )
endif()

add_executable(omnisight-pos-hsm-safenet-luna-client-smoke
  ${CMAKE_CURRENT_LIST_DIR}/safenet-luna-client.cpp
)
target_compile_definitions(omnisight-pos-hsm-safenet-luna-client-smoke PRIVATE
  OMNISIGHT_SAFENET_LUNA_CLIENT_SMOKE_MAIN=1
)
target_compile_features(omnisight-pos-hsm-safenet-luna-client-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-pos-hsm-safenet-luna-client-smoke PRIVATE -Wall -Wextra -Werror)
target_link_libraries(omnisight-pos-hsm-safenet-luna-client-smoke PRIVATE
  omnisight-pos-hsm-client-registry
)
