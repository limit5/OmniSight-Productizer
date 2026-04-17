/** @type {Detox.DetoxConfig} */
module.exports = {
  testRunner: {
    args: {
      $0: 'jest',
      config: 'e2e/jest.config.js',
    },
    jest: {
      setupTimeout: 120000,
    },
  },
  apps: {
    'ios.release': {
      type: 'ios.app',
      binaryPath: 'ios/build/Build/Products/Release-iphonesimulator/App.app',
      build:
        'xcodebuild -workspace ios/App.xcworkspace -scheme App -configuration Release -sdk iphonesimulator -derivedDataPath ios/build',
    },
    'android.release': {
      type: 'android.apk',
      binaryPath: 'android/app/build/outputs/apk/release/app-release.apk',
      build:
        'cd android && ./gradlew assembleRelease assembleAndroidTest -DtestBuildType=release',
    },
  },
  devices: {
    simulator: {
      type: 'ios.simulator',
      device: {type: 'iPhone 15 Pro'},
    },
    emulator: {
      type: 'android.emulator',
      device: {avdName: 'omnisight_pixel8_api34'},
    },
  },
  configurations: {
    'ios.sim.release': {device: 'simulator', app: 'ios.release'},
    'android.emu.release': {device: 'emulator', app: 'android.release'},
  },
};
