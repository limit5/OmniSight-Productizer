# Case 7 Zebra SNAPI barcode scanner CMake fragment (OP-2034).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.
# Zebra SNAPI SDK blobs are fetched from the NDA mirror during builds; the
# static-link path targets RK3588 aarch64 in the same shape as Case 5 vendor
# SDK adapters.

add_library(omnisight-pos-zebra-snapi-adapter STATIC
  ${CMAKE_CURRENT_LIST_DIR}/zebra-snapi-adapter.cpp
)

target_include_directories(omnisight-pos-zebra-snapi-adapter PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-pos-zebra-snapi-adapter PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-pos-zebra-snapi-adapter PRIVATE
  -Wall
  -Wextra
  -Werror
)

set(OMNISIGHT_ZEBRA_SNAPI_SDK_ROOT "" CACHE PATH
  "NDA-mirror Zebra SNAPI SDK root for static-link builds")

if(OMNISIGHT_ZEBRA_SNAPI_SDK_ROOT)
  find_library(OMNISIGHT_ZEBRA_SNAPI_LIBRARY
    NAMES libzebra_snapi.a libsnapi.a
    PATHS ${OMNISIGHT_ZEBRA_SNAPI_SDK_ROOT}/lib
    NO_DEFAULT_PATH
    REQUIRED
  )

  target_compile_definitions(omnisight-pos-zebra-snapi-adapter PRIVATE
    OMNISIGHT_ZEBRA_SNAPI_WITH_SDK=1
  )
  target_include_directories(omnisight-pos-zebra-snapi-adapter PRIVATE
    ${OMNISIGHT_ZEBRA_SNAPI_SDK_ROOT}/include
  )
  target_link_directories(omnisight-pos-zebra-snapi-adapter PUBLIC
    ${OMNISIGHT_ZEBRA_SNAPI_SDK_ROOT}/lib
  )
  target_link_libraries(omnisight-pos-zebra-snapi-adapter PUBLIC
    ${OMNISIGHT_ZEBRA_SNAPI_LIBRARY}
  )
endif()

add_executable(omnisight-pos-zebra-snapi-adapter-smoke
  ${CMAKE_CURRENT_LIST_DIR}/zebra-snapi-adapter.cpp
)
target_compile_definitions(omnisight-pos-zebra-snapi-adapter-smoke PRIVATE
  OMNISIGHT_ZEBRA_SNAPI_ADAPTER_SMOKE_MAIN=1
)
target_compile_features(omnisight-pos-zebra-snapi-adapter-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-pos-zebra-snapi-adapter-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
target_include_directories(omnisight-pos-zebra-snapi-adapter-smoke PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
)
if(OMNISIGHT_ZEBRA_SNAPI_SDK_ROOT)
  target_compile_definitions(omnisight-pos-zebra-snapi-adapter-smoke PRIVATE
    OMNISIGHT_ZEBRA_SNAPI_WITH_SDK=1
  )
  target_include_directories(omnisight-pos-zebra-snapi-adapter-smoke PRIVATE
    ${OMNISIGHT_ZEBRA_SNAPI_SDK_ROOT}/include
  )
  target_link_libraries(omnisight-pos-zebra-snapi-adapter-smoke PRIVATE
    ${OMNISIGHT_ZEBRA_SNAPI_LIBRARY}
  )
endif()
