# Case 5 WebRTC CMake fragment (OP-1986, OP-1992).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-conference-webrtc-peer-connection STATIC
  ${CMAKE_CURRENT_LIST_DIR}/peer-connection.cpp
)

add_library(omnisight-conference-webrtc-ice-client STATIC
  ${CMAKE_CURRENT_LIST_DIR}/ice-client.cpp
)

target_include_directories(omnisight-conference-webrtc-peer-connection PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_include_directories(omnisight-conference-webrtc-ice-client PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-conference-webrtc-peer-connection PUBLIC
  cxx_std_17
)
target_compile_features(omnisight-conference-webrtc-ice-client PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-conference-webrtc-peer-connection PRIVATE
  -Wall
  -Wextra
  -Werror
)
target_compile_options(omnisight-conference-webrtc-ice-client PRIVATE
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
