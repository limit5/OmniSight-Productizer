# Case 5 SIP signaling + RTP/RTCP media plane CMake fragment (OP-2001, OP-2004).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-conference-sip-signaling STATIC
  ${CMAKE_CURRENT_LIST_DIR}/signaling.cpp
)

target_include_directories(omnisight-conference-sip-signaling PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-conference-sip-signaling PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-conference-sip-signaling PRIVATE
  -Wall
  -Wextra
  -Werror
)

add_library(omnisight-conference-sip-media STATIC
  ${CMAKE_CURRENT_LIST_DIR}/media.cpp
)

target_include_directories(omnisight-conference-sip-media PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-conference-sip-media PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-conference-sip-media PRIVATE
  -Wall
  -Wextra
  -Werror
)

find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(PJSIP QUIET libpjproject)
endif()

if(PJSIP_FOUND)
  target_compile_definitions(omnisight-conference-sip-signaling PRIVATE
    OMNISIGHT_SIP_WITH_PJSIP=1
  )
  target_include_directories(omnisight-conference-sip-signaling PRIVATE
    ${PJSIP_INCLUDE_DIRS}
  )
  target_link_libraries(omnisight-conference-sip-signaling PUBLIC
    ${PJSIP_LIBRARIES}
  )
  target_link_directories(omnisight-conference-sip-signaling PUBLIC
    ${PJSIP_LIBRARY_DIRS}
  )
  target_compile_options(omnisight-conference-sip-signaling PRIVATE
    ${PJSIP_CFLAGS_OTHER}
  )

  # OP-2004 media plane uses pjsip's RTP/RTCP modules when available.
  target_compile_definitions(omnisight-conference-sip-media PRIVATE
    OMNISIGHT_SIP_WITH_PJSIP=1
  )
  target_include_directories(omnisight-conference-sip-media PRIVATE
    ${PJSIP_INCLUDE_DIRS}
  )
  target_link_libraries(omnisight-conference-sip-media PUBLIC
    ${PJSIP_LIBRARIES}
  )
  target_link_directories(omnisight-conference-sip-media PUBLIC
    ${PJSIP_LIBRARY_DIRS}
  )
  target_compile_options(omnisight-conference-sip-media PRIVATE
    ${PJSIP_CFLAGS_OTHER}
  )
endif()
