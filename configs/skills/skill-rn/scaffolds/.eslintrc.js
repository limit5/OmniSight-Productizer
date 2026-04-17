// P4 react-native role anti-pattern lock-in via lint rules.
module.exports = {
  root: true,
  extends: ['@react-native'],
  rules: {
    // No raw console logs in shipped code — switch to react-native-logs
    // or Flipper. Roles: configs/roles/mobile/react-native.skill.md.
    'no-console': ['error', {allow: ['warn', 'error']}],
    'react-hooks/exhaustive-deps': 'error',
  },
};
