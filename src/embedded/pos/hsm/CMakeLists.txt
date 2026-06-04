# Case 7 Thales nShield HSM client CMake fragment (OP-2027).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.
# The nShield static-link path uses SDK artifacts from the NDA mirror when
# OMNISIGHT_THALES_NSHIELD_SDK_ROOT is set; package-level container fallback
# remains outside this wrapper when vendor runtime invariants reject static
# linking.

add_library(omnisight-pos-hsm-thales-nshield-client STATIC
  ${CMAKE_CURRENT_LIST_DIR}/thales-nshield-client.cpp
)

target_include_directories(omnisight-pos-hsm-thales-nshield-client PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-pos-hsm-thales-nshield-client PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-pos-hsm-thales-nshield-client PRIVATE
  -Wall
  -Wextra
  -Werror
)

set(OMNISIGHT_THALES_NSHIELD_SDK_ROOT "" CACHE PATH
  "NDA-mirror Thales nShield SDK root for static-link builds")

if(OMNISIGHT_THALES_NSHIELD_SDK_ROOT)
  find_library(OMNISIGHT_THALES_NSHIELD_PKCS11_LIBRARY
    NAMES libcknfast.a cknfast
    PATHS ${OMNISIGHT_THALES_NSHIELD_SDK_ROOT}/lib
    NO_DEFAULT_PATH
  )
  find_library(OMNISIGHT_THALES_NSHIELD_NFAST_LIBRARY
    NAMES libnfast.a nfast
    PATHS ${OMNISIGHT_THALES_NSHIELD_SDK_ROOT}/lib
    NO_DEFAULT_PATH
  )

  target_compile_definitions(omnisight-pos-hsm-thales-nshield-client PRIVATE
    OMNISIGHT_THALES_NSHIELD_WITH_SDK=1
  )
  target_include_directories(omnisight-pos-hsm-thales-nshield-client PRIVATE
    ${OMNISIGHT_THALES_NSHIELD_SDK_ROOT}/include
  )
  target_link_directories(omnisight-pos-hsm-thales-nshield-client PUBLIC
    ${OMNISIGHT_THALES_NSHIELD_SDK_ROOT}/lib
  )
  if(OMNISIGHT_THALES_NSHIELD_PKCS11_LIBRARY)
    target_link_libraries(omnisight-pos-hsm-thales-nshield-client PUBLIC
      ${OMNISIGHT_THALES_NSHIELD_PKCS11_LIBRARY}
    )
  endif()
  if(OMNISIGHT_THALES_NSHIELD_NFAST_LIBRARY)
    target_link_libraries(omnisight-pos-hsm-thales-nshield-client PUBLIC
      ${OMNISIGHT_THALES_NSHIELD_NFAST_LIBRARY}
    )
  endif()
endif()

add_executable(omnisight-pos-hsm-thales-nshield-client-smoke
  ${CMAKE_CURRENT_LIST_DIR}/thales-nshield-client.cpp
)
target_compile_definitions(omnisight-pos-hsm-thales-nshield-client-smoke PRIVATE
  OMNISIGHT_THALES_NSHIELD_CLIENT_SMOKE_MAIN=1
)
target_compile_features(omnisight-pos-hsm-thales-nshield-client-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-pos-hsm-thales-nshield-client-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
target_include_directories(omnisight-pos-hsm-thales-nshield-client-smoke PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
)
