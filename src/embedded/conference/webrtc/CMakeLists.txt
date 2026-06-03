# Case 5 WebRTC CMake fragment (OP-1986, OP-1992, OP-1995).
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

add_library(omnisight-conference-webrtc-outbound-media STATIC
  ${CMAKE_CURRENT_LIST_DIR}/outbound-media.cpp
)

target_include_directories(omnisight-conference-webrtc-outbound-media PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-conference-webrtc-outbound-media PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-conference-webrtc-outbound-media PRIVATE
  -Wall
  -Wextra
  -Werror
)
target_link_libraries(omnisight-conference-webrtc-outbound-media PUBLIC
  omnisight-conference-webrtc-peer-connection
)

if(PkgConfig_FOUND)
  pkg_check_modules(OPUS QUIET opus)
endif()

if(OPUS_FOUND)
  target_compile_definitions(omnisight-conference-webrtc-outbound-media PRIVATE
    OMNISIGHT_WEBRTC_WITH_OPUS=1
  )
  target_include_directories(omnisight-conference-webrtc-outbound-media PRIVATE
    ${OPUS_INCLUDE_DIRS}
  )
  target_link_libraries(omnisight-conference-webrtc-outbound-media PUBLIC
    ${OPUS_LIBRARIES}
  )
  target_link_directories(omnisight-conference-webrtc-outbound-media PUBLIC
    ${OPUS_LIBRARY_DIRS}
  )
  target_compile_options(omnisight-conference-webrtc-outbound-media PRIVATE
    ${OPUS_CFLAGS_OTHER}
  )
endif()

add_executable(omnisight-conference-webrtc-outbound-media-smoke
  ${CMAKE_CURRENT_LIST_DIR}/outbound-media.cpp
)
target_compile_definitions(omnisight-conference-webrtc-outbound-media-smoke PRIVATE
  OMNISIGHT_OUTBOUND_MEDIA_SMOKE_MAIN=1
)
target_link_libraries(omnisight-conference-webrtc-outbound-media-smoke PRIVATE
  omnisight-conference-webrtc-peer-connection
)
target_compile_features(omnisight-conference-webrtc-outbound-media-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-conference-webrtc-outbound-media-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
if(OPUS_FOUND)
  target_compile_definitions(omnisight-conference-webrtc-outbound-media-smoke PRIVATE
    OMNISIGHT_WEBRTC_WITH_OPUS=1
  )
  target_include_directories(omnisight-conference-webrtc-outbound-media-smoke PRIVATE
    ${OPUS_INCLUDE_DIRS}
  )
  target_link_libraries(omnisight-conference-webrtc-outbound-media-smoke PRIVATE
    ${OPUS_LIBRARIES}
  )
  target_link_directories(omnisight-conference-webrtc-outbound-media-smoke PRIVATE
    ${OPUS_LIBRARY_DIRS}
  )
  target_compile_options(omnisight-conference-webrtc-outbound-media-smoke PRIVATE
    ${OPUS_CFLAGS_OTHER}
  )
endif()
