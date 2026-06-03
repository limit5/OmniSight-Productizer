# Case 5 Zoom Linux Meeting SDK CMake fragment (OP-2005).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.
# Zoom's Linux SDK is fetched from the NDA mirror during builds; the static
# link path bridges the SDK's glibc 2.31 ABI on RK3588 aarch64. If the SDK's
# runtime invariants reject static linking, package-level container fallback
# remains outside this adapter.

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
