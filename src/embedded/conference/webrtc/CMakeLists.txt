# Case 5 WebRTC PeerConnection CMake fragment (OP-1986).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-conference-webrtc-peer-connection STATIC
  ${CMAKE_CURRENT_LIST_DIR}/peer-connection.cpp
)

target_include_directories(omnisight-conference-webrtc-peer-connection PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-conference-webrtc-peer-connection PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-conference-webrtc-peer-connection PRIVATE
  -Wall
  -Wextra
  -Werror
)

find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(LIBDATACHANNEL QUIET libdatachannel)
endif()

if(LIBDATACHANNEL_FOUND)
  target_compile_definitions(omnisight-conference-webrtc-peer-connection PRIVATE
    OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL=1
  )
  target_include_directories(omnisight-conference-webrtc-peer-connection PRIVATE
    ${LIBDATACHANNEL_INCLUDE_DIRS}
  )
  target_link_libraries(omnisight-conference-webrtc-peer-connection PUBLIC
    ${LIBDATACHANNEL_LIBRARIES}
  )
  target_link_directories(omnisight-conference-webrtc-peer-connection PUBLIC
    ${LIBDATACHANNEL_LIBRARY_DIRS}
  )
  target_compile_options(omnisight-conference-webrtc-peer-connection PRIVATE
    ${LIBDATACHANNEL_CFLAGS_OTHER}
  )
endif()
