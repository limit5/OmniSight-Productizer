# Cases 6+7 UAV optical flow CMake fragment (OP-2031).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-uav-cv-optical-flow STATIC
  ${CMAKE_CURRENT_LIST_DIR}/optical-flow.cpp
)

target_include_directories(omnisight-uav-cv-optical-flow PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-uav-cv-optical-flow PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-uav-cv-optical-flow PRIVATE
  -Wall
  -Wextra
  -Werror
)

find_package(OpenCV QUIET COMPONENTS core imgproc video)
if(OpenCV_FOUND)
  target_compile_definitions(omnisight-uav-cv-optical-flow PRIVATE
    OMNISIGHT_UAV_CV_WITH_OPENCV=1
  )
  target_include_directories(omnisight-uav-cv-optical-flow PRIVATE
    ${OpenCV_INCLUDE_DIRS}
  )
  target_link_libraries(omnisight-uav-cv-optical-flow PUBLIC
    ${OpenCV_LIBS}
  )
endif()

add_executable(omnisight-uav-cv-optical-flow-smoke
  ${CMAKE_CURRENT_LIST_DIR}/optical-flow.cpp
)
target_compile_definitions(omnisight-uav-cv-optical-flow-smoke PRIVATE
  OMNISIGHT_UAV_OPTICAL_FLOW_SMOKE_MAIN=1
)
target_compile_features(omnisight-uav-cv-optical-flow-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-uav-cv-optical-flow-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
if(OpenCV_FOUND)
  target_compile_definitions(omnisight-uav-cv-optical-flow-smoke PRIVATE
    OMNISIGHT_UAV_CV_WITH_OPENCV=1
  )
  target_include_directories(omnisight-uav-cv-optical-flow-smoke PRIVATE
    ${OpenCV_INCLUDE_DIRS}
  )
  target_link_libraries(omnisight-uav-cv-optical-flow-smoke PRIVATE
    ${OpenCV_LIBS}
  )
endif()
