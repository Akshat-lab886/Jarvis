/// Pair-link scheme shared with the Mac hub dashboard.
///
/// After minting a code, the dashboard shows a link of the form:
///   jarvis://pair?hub=<url-encoded hub origin>&code=<8-CHAR CODE>
/// e.g. jarvis://pair?hub=http%3A%2F%2F192.168.1.5%3A5001&code=7KQ2PX4M
///
/// QR-scan, paste-from-clipboard, and manual entry all funnel through
/// [parsePairUri]. The link carries no secret — the 8-char code is
/// single-use with a 10-min TTL and redeems over the SAME
/// POST /api/mobile/redeem endpoint as the PWA. No new protocol,
/// no new trust. Pure Dart, unit-tested (test/pair_uri_test.dart).
class PairInfo {
  final String hub;
  final String code;
  const PairInfo({required this.hub, required this.code});
}

// Same alphabet as the hub (utils/mobile_link.py _CODE_ALPHABET):
// no 0/O, 1/I/l so codes survive manual readout.
final RegExp _codePattern =
    RegExp(r'^[23456789ABCDEFGHJKMNPQRSTUWXYZ]{8}$');

String normalizeHub(String raw) =>
    raw.trim().replaceAll(RegExp(r'/+$'), '');

/// Parse a jarvis://pair link. Returns null for anything malformed —
/// wrong scheme/host, missing hub, non-http(s) hub, or a code outside
/// the hub alphabet (never throws).
PairInfo? parsePairUri(String input) {
  final text = input.trim();
  if (text.isEmpty) return null;
  Uri? uri;
  try {
    uri = Uri.parse(text);
  } catch (_) {
    return null;
  }
  if (uri.scheme != 'jarvis' || uri.host != 'pair') return null;
  final hub = normalizeHub(uri.queryParameters['hub'] ?? '');
  final code = (uri.queryParameters['code'] ?? '').trim().toUpperCase();
  if (hub.isEmpty || !_codePattern.hasMatch(code)) return null;
  if (!hub.startsWith('http://') && !hub.startsWith('https://')) {
    return null;
  }
  return PairInfo(hub: hub, code: code);
}

/// Build a pair link (used by tests + the paste-link round-trip check).
String buildPairUri({required String hub, required String code}) {
  final h = normalizeHub(hub);
  final c = code.trim().toUpperCase();
  return 'jarvis://pair?hub=${Uri.encodeComponent(h)}'
      '&code=${Uri.encodeComponent(c)}';
}
