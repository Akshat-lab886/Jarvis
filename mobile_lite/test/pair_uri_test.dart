import 'package:flutter_test/flutter_test.dart';
import 'package:jarvis_lite/core/intent.dart';
import 'package:jarvis_lite/core/pair_uri.dart';

void main() {
  group('parsePairUri', () {
    test('parses a well-formed pair link', () {
      final info = parsePairUri(
          'jarvis://pair?hub=http%3A%2F%2F192.168.1.5%3A5001&code=7KQ2PX4M');
      expect(info, isNotNull);
      expect(info!.hub, 'http://192.168.1.5:5001');
      expect(info.code, '7KQ2PX4M');
    });

    test('lowercase code is uppercased', () {
      final info = parsePairUri('jarvis://pair?hub=http://x:5001&code=7kq2px4m');
      expect(info?.code, '7KQ2PX4M');
    });

    test('trailing slashes trimmed from hub', () {
      final info =
          parsePairUri('jarvis://pair?hub=http://x:5001//&code=7KQ2PX4M');
      expect(info?.hub, 'http://x:5001');
    });

    test('rejects wrong scheme, host, hub, code', () {
      expect(parsePairUri('https://pair?hub=http://x&code=7KQ2PX4M'), isNull);
      expect(parsePairUri('jarvis://other?hub=http://x&code=7KQ2PX4M'), isNull);
      expect(parsePairUri('jarvis://pair?hub=&code=7KQ2PX4M'), isNull);
      expect(
          parsePairUri('jarvis://pair?hub=ftp://x&code=7KQ2PX4M'), isNull);
      // Look-alike letters outside the hub alphabet never parse.
      expect(parsePairUri('jarvis://pair?hub=http://x&code=00000000'), isNull);
      expect(parsePairUri('jarvis://pair?hub=http://x&code=SHORT'), isNull);
      expect(parsePairUri('jarvis://pair?hub=http://x'), isNull);
      expect(parsePairUri('not a link at all'), isNull);
      expect(parsePairUri(''), isNull);
    });

    test('build/parse round-trip', () {
      final uri =
          buildPairUri(hub: 'http://192.168.1.5:5001/', code: '7kq2px4m');
      final info = parsePairUri(uri);
      expect(info?.hub, 'http://192.168.1.5:5001');
      expect(info?.code, '7KQ2PX4M');
    });
  });

  group('routeInput', () {
    test('/note and note: prefixes stay offline', () {
      expect(routeInput('/note buy milk').kind, LocalIntent.note);
      expect(routeInput('/note buy milk').text, 'buy milk');
      expect(routeInput('note: buy milk').kind, LocalIntent.note);
      expect(routeInput('NOTE: buy milk').text, 'buy milk');
    });

    test('everything else goes to hub chat', () {
      expect(routeInput('remind me at 5pm').kind, LocalIntent.chat);
      expect(routeInput('delete all my files').kind, LocalIntent.chat);
      expect(routeInput('noteworthy idea').kind, LocalIntent.chat);
    });
  });
}
