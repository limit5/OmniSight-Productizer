# Case 7 EMV Verifone payment terminal vendor adapter CMake fragment
# (OP-2054).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.
# Verifone SDK archives are fetched from the OP-491 NDA mirror for static-link
# builds.

add_library(omnisight-pos-emv-payment-terminal-adapter INTERFACE)
target_include_directories(omnisight-pos-emv-payment-terminal-adapter INTERFACE
  ${CMAKE_CURRENT_LIST_DIR}
  ${CMAKE_CURRENT_LIST_DIR}/..
)
target_compile_features(omnisight-pos-emv-payment-terminal-adapter INTERFACE
  cxx_std_17
)

add_library(omnisight-pos-emv-verifone-adapter STATIC
  ${CMAKE_CURRENT_LIST_DIR}/verifone-adapter.cpp
)
target_include_directories(omnisight-pos-emv-verifone-adapter PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
  ${CMAKE_CURRENT_LIST_DIR}/..
)
target_compile_features(omnisight-pos-emv-verifone-adapter PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-pos-emv-verifone-adapter PRIVATE
  -Wall
  -Wextra
  -Werror
)
target_link_libraries(omnisight-pos-emv-verifone-adapter PUBLIC
  omnisight-pos-emv-payment-terminal-adapter
  omnisight-pos-emv-iso7816-contact-reader
  omnisight-pos-emv-iso14443-contactless-reader
  omnisight-pos-emv-ped-pin-entry
  omnisight-pos-emv-cryptogram-generator
)

set(OMNISIGHT_VERIFONE_SDK_ROOT "" CACHE PATH
  "OP-491 NDA-mirror Verifone VerixOS SDK root for static-link builds")

if(OMNISIGHT_VERIFONE_SDK_ROOT)
  find_library(OMNISIGHT_VERIFONE_VERIXOS_LIBRARY
    NAMES libverifone_verixos.a libverixos.a verifone_verixos verixos
    PATHS ${OMNISIGHT_VERIFONE_SDK_ROOT}/lib
    NO_DEFAULT_PATH
    REQUIRED
  )

  target_compile_definitions(omnisight-pos-emv-verifone-adapter PRIVATE
    OMNISIGHT_VERIFONE_WITH_SDK=1
  )
  target_include_directories(omnisight-pos-emv-verifone-adapter PRIVATE
    ${OMNISIGHT_VERIFONE_SDK_ROOT}/include
  )
  target_link_directories(omnisight-pos-emv-verifone-adapter PUBLIC
    ${OMNISIGHT_VERIFONE_SDK_ROOT}/lib
  )
  target_link_libraries(omnisight-pos-emv-verifone-adapter PUBLIC
    ${OMNISIGHT_VERIFONE_VERIXOS_LIBRARY}
  )
endif()

add_executable(omnisight-pos-emv-verifone-adapter-smoke
  ${CMAKE_CURRENT_LIST_DIR}/verifone-adapter.cpp
)
target_compile_definitions(omnisight-pos-emv-verifone-adapter-smoke PRIVATE
  OMNISIGHT_VERIFONE_ADAPTER_SMOKE_MAIN=1
)
target_include_directories(omnisight-pos-emv-verifone-adapter-smoke PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
  ${CMAKE_CURRENT_LIST_DIR}/..
)
target_compile_features(omnisight-pos-emv-verifone-adapter-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-pos-emv-verifone-adapter-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
target_link_libraries(omnisight-pos-emv-verifone-adapter-smoke PRIVATE
  omnisight-pos-emv-iso7816-contact-reader
  omnisight-pos-emv-iso14443-contactless-reader
  omnisight-pos-emv-ped-pin-entry
  omnisight-pos-emv-cryptogram-generator
)

if(OMNISIGHT_VERIFONE_SDK_ROOT)
  target_compile_definitions(omnisight-pos-emv-verifone-adapter-smoke PRIVATE
    OMNISIGHT_VERIFONE_WITH_SDK=1
  )
  target_include_directories(omnisight-pos-emv-verifone-adapter-smoke PRIVATE
    ${OMNISIGHT_VERIFONE_SDK_ROOT}/include
  )
  target_link_libraries(omnisight-pos-emv-verifone-adapter-smoke PRIVATE
    ${OMNISIGHT_VERIFONE_VERIXOS_LIBRARY}
  )
endif()

# -- PAX Prolin OS adapter (OP-2057) --------------------------------

add_library(omnisight-pos-emv-pax-adapter STATIC
  ${CMAKE_CURRENT_LIST_DIR}/pax-adapter.cpp
)
target_include_directories(omnisight-pos-emv-pax-adapter PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
  ${CMAKE_CURRENT_LIST_DIR}/..
)
target_compile_features(omnisight-pos-emv-pax-adapter PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-pos-emv-pax-adapter PRIVATE
  -Wall
  -Wextra
  -Werror
)
target_link_libraries(omnisight-pos-emv-pax-adapter PUBLIC
  omnisight-pos-emv-payment-terminal-adapter
  omnisight-pos-emv-iso7816-contact-reader
  omnisight-pos-emv-iso14443-contactless-reader
  omnisight-pos-emv-ped-pin-entry
  omnisight-pos-emv-cryptogram-generator
)

set(OMNISIGHT_PAX_PROLIN_SDK_ROOT "" CACHE PATH
  "OP-491 NDA-mirror PAX Prolin OS SDK root for static-link builds")

if(OMNISIGHT_PAX_PROLIN_SDK_ROOT)
  find_library(OMNISIGHT_PAX_PROLIN_LIBRARY
    NAMES libpax_prolin.a libprolin.a pax_prolin prolin
    PATHS ${OMNISIGHT_PAX_PROLIN_SDK_ROOT}/lib
    NO_DEFAULT_PATH
    REQUIRED
  )

  target_compile_definitions(omnisight-pos-emv-pax-adapter PRIVATE
    OMNISIGHT_PAX_WITH_PROLIN_SDK=1
  )
  target_include_directories(omnisight-pos-emv-pax-adapter PRIVATE
    ${OMNISIGHT_PAX_PROLIN_SDK_ROOT}/include
  )
  target_link_directories(omnisight-pos-emv-pax-adapter PUBLIC
    ${OMNISIGHT_PAX_PROLIN_SDK_ROOT}/lib
  )
  target_link_libraries(omnisight-pos-emv-pax-adapter PUBLIC
    ${OMNISIGHT_PAX_PROLIN_LIBRARY}
  )
endif()

add_executable(omnisight-pos-emv-pax-adapter-smoke
  ${CMAKE_CURRENT_LIST_DIR}/pax-adapter.cpp
)
target_compile_definitions(omnisight-pos-emv-pax-adapter-smoke PRIVATE
  OMNISIGHT_PAX_ADAPTER_SMOKE_MAIN=1
)
target_include_directories(omnisight-pos-emv-pax-adapter-smoke PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
  ${CMAKE_CURRENT_LIST_DIR}/..
)
target_compile_features(omnisight-pos-emv-pax-adapter-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-pos-emv-pax-adapter-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
target_link_libraries(omnisight-pos-emv-pax-adapter-smoke PRIVATE
  omnisight-pos-emv-iso7816-contact-reader
  omnisight-pos-emv-iso14443-contactless-reader
  omnisight-pos-emv-ped-pin-entry
  omnisight-pos-emv-cryptogram-generator
)

if(OMNISIGHT_PAX_PROLIN_SDK_ROOT)
  target_compile_definitions(omnisight-pos-emv-pax-adapter-smoke PRIVATE
    OMNISIGHT_PAX_WITH_PROLIN_SDK=1
  )
  target_include_directories(omnisight-pos-emv-pax-adapter-smoke PRIVATE
    ${OMNISIGHT_PAX_PROLIN_SDK_ROOT}/include
  )
  target_link_libraries(omnisight-pos-emv-pax-adapter-smoke PRIVATE
    ${OMNISIGHT_PAX_PROLIN_LIBRARY}
  )
endif()

# -- Ingenico Telium/TelGea adapter (OP-2057) -----------------------

add_library(omnisight-pos-emv-ingenico-adapter STATIC
  ${CMAKE_CURRENT_LIST_DIR}/ingenico-adapter.cpp
)
target_include_directories(omnisight-pos-emv-ingenico-adapter PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
  ${CMAKE_CURRENT_LIST_DIR}/..
)
target_compile_features(omnisight-pos-emv-ingenico-adapter PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-pos-emv-ingenico-adapter PRIVATE
  -Wall
  -Wextra
  -Werror
)
target_link_libraries(omnisight-pos-emv-ingenico-adapter PUBLIC
  omnisight-pos-emv-payment-terminal-adapter
  omnisight-pos-emv-iso7816-contact-reader
  omnisight-pos-emv-iso14443-contactless-reader
  omnisight-pos-emv-ped-pin-entry
  omnisight-pos-emv-cryptogram-generator
)

set(OMNISIGHT_INGENICO_TELIUM_SDK_ROOT "" CACHE PATH
  "OP-491 NDA-mirror Ingenico Telium/TelGea SDK root for static-link builds")

if(OMNISIGHT_INGENICO_TELIUM_SDK_ROOT)
  find_library(OMNISIGHT_INGENICO_TELIUM_LIBRARY
    NAMES libingenico_telium.a libtelium.a ingenico_telium telium
    PATHS ${OMNISIGHT_INGENICO_TELIUM_SDK_ROOT}/lib
    NO_DEFAULT_PATH
    REQUIRED
  )
  find_library(OMNISIGHT_INGENICO_TELGEA_LIBRARY
    NAMES libingenico_telgea.a libtelgea.a ingenico_telgea telgea
    PATHS ${OMNISIGHT_INGENICO_TELIUM_SDK_ROOT}/lib
    NO_DEFAULT_PATH
    REQUIRED
  )

  target_compile_definitions(omnisight-pos-emv-ingenico-adapter PRIVATE
    OMNISIGHT_INGENICO_WITH_TELIUM_SDK=1
  )
  target_include_directories(omnisight-pos-emv-ingenico-adapter PRIVATE
    ${OMNISIGHT_INGENICO_TELIUM_SDK_ROOT}/include
  )
  target_link_directories(omnisight-pos-emv-ingenico-adapter PUBLIC
    ${OMNISIGHT_INGENICO_TELIUM_SDK_ROOT}/lib
  )
  target_link_libraries(omnisight-pos-emv-ingenico-adapter PUBLIC
    ${OMNISIGHT_INGENICO_TELIUM_LIBRARY}
    ${OMNISIGHT_INGENICO_TELGEA_LIBRARY}
  )
endif()

add_executable(omnisight-pos-emv-ingenico-adapter-smoke
  ${CMAKE_CURRENT_LIST_DIR}/ingenico-adapter.cpp
)
target_compile_definitions(omnisight-pos-emv-ingenico-adapter-smoke PRIVATE
  OMNISIGHT_INGENICO_ADAPTER_SMOKE_MAIN=1
)
target_include_directories(omnisight-pos-emv-ingenico-adapter-smoke PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
  ${CMAKE_CURRENT_LIST_DIR}/..
)
target_compile_features(omnisight-pos-emv-ingenico-adapter-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-pos-emv-ingenico-adapter-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
target_link_libraries(omnisight-pos-emv-ingenico-adapter-smoke PRIVATE
  omnisight-pos-emv-iso7816-contact-reader
  omnisight-pos-emv-iso14443-contactless-reader
  omnisight-pos-emv-ped-pin-entry
  omnisight-pos-emv-cryptogram-generator
)

if(OMNISIGHT_INGENICO_TELIUM_SDK_ROOT)
  target_compile_definitions(omnisight-pos-emv-ingenico-adapter-smoke PRIVATE
    OMNISIGHT_INGENICO_WITH_TELIUM_SDK=1
  )
  target_include_directories(omnisight-pos-emv-ingenico-adapter-smoke PRIVATE
    ${OMNISIGHT_INGENICO_TELIUM_SDK_ROOT}/include
  )
  target_link_libraries(omnisight-pos-emv-ingenico-adapter-smoke PRIVATE
    ${OMNISIGHT_INGENICO_TELIUM_LIBRARY}
    ${OMNISIGHT_INGENICO_TELGEA_LIBRARY}
  )
endif()
