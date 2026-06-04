# Cases 6+7 UAV computer vision CMake fragment (OP-2031 optical flow, OP-2037
# object detect, OP-2046 video stabilization).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

# ── Lucas-Kanade optical flow (OP-2031) ─────────────────────────────

add_library(omnisight-uav-cv-optical-flow STATIC
  ${CMAKE_CURRENT_LIST_DIR}/optical-flow.cpp
)
target_include_directories(omnisight-uav-cv-optical-flow PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-uav-cv-optical-flow PUBLIC cxx_std_17)
target_compile_options(omnisight-uav-cv-optical-flow PRIVATE -Wall -Wextra -Werror)

find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(OPENCV4 QUIET opencv4)
endif()

if(OPENCV4_FOUND)
  target_compile_definitions(omnisight-uav-cv-optical-flow PRIVATE
    OMNISIGHT_CV_WITH_OPENCV=1
  )
  target_include_directories(omnisight-uav-cv-optical-flow PRIVATE
    ${OPENCV4_INCLUDE_DIRS}
  )
  target_link_libraries(omnisight-uav-cv-optical-flow PUBLIC
    ${OPENCV4_LIBRARIES}
  )
  target_link_directories(omnisight-uav-cv-optical-flow PUBLIC
    ${OPENCV4_LIBRARY_DIRS}
  )
  target_compile_options(omnisight-uav-cv-optical-flow PRIVATE
    ${OPENCV4_CFLAGS_OTHER}
  )
endif()

add_executable(omnisight-uav-cv-optical-flow-smoke
  ${CMAKE_CURRENT_LIST_DIR}/optical-flow.cpp
)
target_compile_definitions(omnisight-uav-cv-optical-flow-smoke PRIVATE
  OMNISIGHT_UAV_CV_OPTICAL_FLOW_SMOKE_MAIN=1
)
target_compile_features(omnisight-uav-cv-optical-flow-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-uav-cv-optical-flow-smoke PRIVATE -Wall -Wextra -Werror)

# ── NPU object detection wire-up (OP-2037) ──────────────────────────

add_library(omnisight-uav-cv-object-detection STATIC
  ${CMAKE_CURRENT_LIST_DIR}/object-detection.cpp
)
target_include_directories(omnisight-uav-cv-object-detection PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-uav-cv-object-detection PUBLIC cxx_std_17)
target_compile_options(omnisight-uav-cv-object-detection PRIVATE -Wall -Wextra -Werror)

add_executable(omnisight-uav-cv-object-detection-smoke
  ${CMAKE_CURRENT_LIST_DIR}/object-detection.cpp
)
target_compile_definitions(omnisight-uav-cv-object-detection-smoke PRIVATE
  OMNISIGHT_UAV_CV_OBJECT_DETECTION_SMOKE_MAIN=1
)
target_compile_features(omnisight-uav-cv-object-detection-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-uav-cv-object-detection-smoke PRIVATE -Wall -Wextra -Werror)

# ── Gyro-assisted video stabilization (OP-2046) ────────────────────

add_library(omnisight-uav-cv-video-stabilization STATIC
  ${CMAKE_CURRENT_LIST_DIR}/video-stabilization.cpp
)
target_include_directories(omnisight-uav-cv-video-stabilization PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-uav-cv-video-stabilization PUBLIC cxx_std_17)
target_compile_options(omnisight-uav-cv-video-stabilization PRIVATE -Wall -Wextra -Werror)

if(OPENCV4_FOUND)
  target_compile_definitions(omnisight-uav-cv-video-stabilization PRIVATE
    OMNISIGHT_CV_WITH_OPENCV=1
  )
  target_include_directories(omnisight-uav-cv-video-stabilization PRIVATE
    ${OPENCV4_INCLUDE_DIRS}
  )
  target_link_libraries(omnisight-uav-cv-video-stabilization PUBLIC
    ${OPENCV4_LIBRARIES}
  )
  target_link_directories(omnisight-uav-cv-video-stabilization PUBLIC
    ${OPENCV4_LIBRARY_DIRS}
  )
  target_compile_options(omnisight-uav-cv-video-stabilization PRIVATE
    ${OPENCV4_CFLAGS_OTHER}
  )
endif()

add_executable(omnisight-uav-cv-video-stabilization-smoke
  ${CMAKE_CURRENT_LIST_DIR}/video-stabilization.cpp
)
target_compile_definitions(omnisight-uav-cv-video-stabilization-smoke PRIVATE
  OMNISIGHT_UAV_CV_VIDEO_STABILIZATION_SMOKE_MAIN=1
)
target_compile_features(omnisight-uav-cv-video-stabilization-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-uav-cv-video-stabilization-smoke PRIVATE -Wall -Wextra -Werror)
if(OPENCV4_FOUND)
  target_include_directories(omnisight-uav-cv-video-stabilization-smoke PRIVATE
    ${OPENCV4_INCLUDE_DIRS}
  )
  target_link_libraries(omnisight-uav-cv-video-stabilization-smoke PRIVATE
    ${OPENCV4_LIBRARIES}
  )
  target_link_directories(omnisight-uav-cv-video-stabilization-smoke PRIVATE
    ${OPENCV4_LIBRARY_DIRS}
  )
  target_compile_options(omnisight-uav-cv-video-stabilization-smoke PRIVATE
    ${OPENCV4_CFLAGS_OTHER}
  )
endif()
