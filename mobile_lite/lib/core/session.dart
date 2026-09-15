import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Credential + pointer store.
///
/// SECURITY: the per-device Bearer token and device id live in the
/// platform keychain (Keystore / Keychain) via flutter_secure_storage.
/// They are NEVER written to SharedPreferences, logs, or the sync DB.
/// Only non-secret pointers (hub URL, device name, sync cursor) use prefs.
class Session {
  static const _kToken = 'jarvis_token';
  static const _kDevice = 'jarvis_device';
  static const _kHub = 'jarvis_hub';
  static const _kName = 'jarvis_name';
  static const _kCursor = 'jarvis_cursor';

  final FlutterSecureStorage _secure = const FlutterSecureStorage(
    aOptions: AndroidOptions(encryptedSharedPreferences: true),
  );

  String hub = '';
  String token = '';
  String deviceId = '';
  String deviceName = 'phone';
  int cursor = 0;

  bool get isPaired => hub.isNotEmpty && token.isNotEmpty;

  Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    hub = prefs.getString(_kHub) ?? '';
    deviceName = prefs.getString(_kName) ?? 'phone';
    cursor = prefs.getInt(_kCursor) ?? 0;
    token = await _secure.read(key: _kToken) ?? '';
    deviceId = await _secure.read(key: _kDevice) ?? '';
  }

  Future<void> savePair({
    required String hub,
    required String token,
    required String deviceId,
    required String name,
  }) async {
    final prefs = await SharedPreferences.getInstance();
    this.hub = hub;
    this.token = token;
    this.deviceId = deviceId;
    deviceName = name;
    cursor = 0;
    await prefs.setString(_kHub, hub);
    await prefs.setString(_kName, name);
    await prefs.setInt(_kCursor, 0);
    await _secure.write(key: _kToken, value: token);
    await _secure.write(key: _kDevice, value: deviceId);
  }

  Future<void> saveCursor(int c) async {
    cursor = c;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setInt(_kCursor, c);
  }

  /// Unpair: wipe secrets first, then pointers. The hub revocation
  /// itself happens on the Mac dashboard (MOBILE LINK → REVOKE).
  Future<void> clear() async {
    hub = '';
    token = '';
    deviceId = '';
    cursor = 0;
    await _secure.delete(key: _kToken);
    await _secure.delete(key: _kDevice);
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_kHub);
    await prefs.remove(_kCursor);
  }
}
