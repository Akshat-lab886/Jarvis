import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'session.dart';

/// Typed errors so the UI can show human sentences, not stack traces.
class HubException implements Exception {
  final String message;
  final int? status;
  const HubException(this.message, [this.status]);
  @override
  String toString() => message;
}

/// Thin client over the hub's /api/mobile/* surface — the SAME surface
/// the PWA uses. No new protocol, no new trust: every call except
/// [redeem] sends the per-device Bearer token, and destructive chat
/// requests still hold for a human on the Mac dashboard (hub-enforced).
class HubApi {
  final Session session;
  final http.Client _http;
  final Duration timeout;

  HubApi(this.session, {http.Client? httpClient, this.timeout = const Duration(seconds: 30)})
      : _http = httpClient ?? http.Client();

  Map<String, String> get _authHeaders => {
        'Content-Type': 'application/json',
        if (session.token.isNotEmpty)
          'Authorization': 'Bearer ${session.token}',
      };

  Future<Map<String, dynamic>> _post(
      String path, Map<String, dynamic> body,
      {bool authed = true}) async {
    final hub = session.hub;
    if (hub.isEmpty) throw const HubException('No hub paired yet.');
    late http.Response res;
    try {
      res = await _http
          .post(Uri.parse('$hub$path'),
              headers: authed
                  ? _authHeaders
                  : const {'Content-Type': 'application/json'},
              body: jsonEncode(body))
          .timeout(timeout);
    } on TimeoutException {
      throw const HubException('Hub timed out — same Wi-Fi / Tailscale on?');
    } catch (e) {
      throw HubException('Could not reach hub: $e');
    }
    Map<String, dynamic> json;
    try {
      json = jsonDecode(res.body) as Map<String, dynamic>;
    } catch (_) {
      throw HubException('Hub answered HTTP ${res.statusCode} (not JSON).',
          res.statusCode);
    }
    if (res.statusCode == 401) {
      throw const HubException(
          'Token rejected — revoked on the dashboard? Unpair and re-pair.', 401);
    }
    if (res.statusCode == 429) {
      throw const HubException(
          'Rate-limited — too many failures, try again in a few minutes.',
          429);
    }
    if (res.statusCode < 200 || res.statusCode >= 300 || json['ok'] == false) {
      throw HubException(
          (json['error'] ?? 'Hub error HTTP ${res.statusCode}').toString(),
          res.statusCode);
    }
    return json;
  }

  /// Exchange a single-use 8-char pairing code for a per-device token.
  /// Returns (token, deviceId). Caller persists via [Session.savePair].
  Future<({String token, String deviceId})> redeem({
    required String hub,
    required String code,
    required String name,
  }) async {
    final remembered = session.hub;
    session.hub = hub;
    try {
      final json = await _post('/api/mobile/redeem',
          {'code': code.trim().toUpperCase(), 'device_name': name},
          authed: false);
      final token = (json['token'] ?? '').toString();
      final deviceId = (json['device_id'] ?? '').toString();
      if (token.isEmpty || deviceId.isEmpty) {
        throw const HubException('Hub redeem reply was empty — retry.');
      }
      return (token: token, deviceId: deviceId);
    } finally {
      if (session.token.isEmpty) session.hub = remembered;
    }
  }

  /// Chat through the SAME think→execute pipeline (host-silent,
  /// HITL-gated on the hub). wait=true → synchronous reply text.
  Future<String> chat(String text) async {
    final clean = text.trim();
    if (clean.isEmpty) throw const HubException('Message is empty.');
    final json = await _post('/api/mobile/chat',
        {'text': clean.substring(0, clean.length > 4000 ? 4000 : clean.length), 'wait': true});
    return (json['result'] ?? 'Done.').toString();
  }

  /// Push up to 100 outbox events + pull hub events newer than [cursor].
  Future<SyncResult> sync(
      {required List<Map<String, dynamic>> events, required int cursor}) async {
    final json = await _post(
        '/api/mobile/sync', {'events': events, 'cursor': cursor});
    final hubEvents = (json['events'] as List? ?? [])
        .whereType<Map>()
        .map((e) => Map<String, dynamic>.from(e))
        .toList();
    return SyncResult(
      applied: (json['applied'] as num? ?? 0).toInt(),
      skipped: (json['skipped'] as num? ?? 0).toInt(),
      cursor: (json['cursor'] as num? ?? cursor).toInt(),
      events: hubEvents,
    );
  }

  /// Fold a one-line context note into hub memory (handoff push).
  Future<void> pushHandoff(String line) async {
    final clean = line.trim();
    if (clean.isEmpty) return;
    await _post('/api/mobile/handoff', {
      'bundle': {
        'history': [
          {'user': '[handoff] $clean'}
        ],
        'goals': [],
      }
    });
  }

  void close() => _http.close();
}

class SyncResult {
  final int applied;
  final int skipped;
  final int cursor;
  final List<Map<String, dynamic>> events;
  const SyncResult(
      {required this.applied,
      required this.skipped,
      required this.cursor,
      required this.events});
}
