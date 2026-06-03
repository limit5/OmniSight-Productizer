# Case 5 audio/video sync daemon CMake fragment (OP-1996).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-av-pair-sync-daemon STATIC
  ${CMAKE_CURRENT_LIST_DIR}/av-sync-daemon.cpp
)

target_include_directories(omnisight-av-pair-sync-daemon PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-av-pair-sync-daemon PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-av-pair-sync-daemon PRIVATE
  -Wall
  -Wextra
  -Werror
)

find_package(Threads REQUIRED)
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(ALSA QUIET alsa)
endif()

include(CheckIncludeFileCXX)
check_include_file_cxx(linux/videodev2.h OMNISIGHT_AV_SYNC_HAS_V4L2_HEADER)

target_link_libraries(omnisight-av-pair-sync-daemon PUBLIC
  Threads::Threads
)

if(ALSA_FOUND AND OMNISIGHT_AV_SYNC_HAS_V4L2_HEADER)
  target_compile_definitions(omnisight-av-pair-sync-daemon PRIVATE
    OMNISIGHT_AV_SYNC_WITH_NATIVE=1
  )
  target_include_directories(omnisight-av-pair-sync-daemon PRIVATE
    ${ALSA_INCLUDE_DIRS}
  )
  target_link_libraries(omnisight-av-pair-sync-daemon PUBLIC
    ${ALSA_LIBRARIES}
  )
  target_link_directories(omnisight-av-pair-sync-daemon PUBLIC
    ${ALSA_LIBRARY_DIRS}
  )
  target_compile_options(omnisight-av-pair-sync-daemon PRIVATE
    ${ALSA_CFLAGS_OTHER}
  )
endif()
