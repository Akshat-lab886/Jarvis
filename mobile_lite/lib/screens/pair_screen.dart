import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../core/hub_api.dart';
import '../core/pair_uri.dart';
import '../core/session.dart';

/// Pairing view — shown when no device token is stored.
///
/// Three ways in (all redeem the SAME single-use 8-char code over
/// POST /api/mobile/redeem, exactly like the PWA):
///   1. SCAN QR — dashboard MOBILE LINK shows jarvis://pair?hub=..&code=..
///   2. PASTE LINK — paste that same link from the clipboard.
///   3. Manual — type hub URL + code by hand.
class PairScreen extends StatefulWidget {
  final Session session;
  final HubApi api;
  final VoidCallback onPaired;

  const PairScreen(
      {super.key,
      required this.session,
      required this.api,
      required this.onPaired});

  @override
  State<PairScreen> createState() => _PairScreenState();
}

class _PairScreenState extends State<PairScreen> {
  final _hub = TextEditingController();
  final _code = TextEditingController();
  final _name = TextEditingController(text: 'phone');
  final _link = TextEditingController();
  bool _busy = false;
  String? _error;
  bool _scanning = false;

  @override
  void dispose() {
    _hub.dispose();
    _code.dispose();
    _name.dispose();
    _link.dispose();
    super.dispose();
  }

  Future<void> _redeem(String hub, String code, String name) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final cleanHub = normalizeHub(hub);
      final cleanCode = code.trim().toUpperCase();
      if (cleanHub.isEmpty || cleanCode.isEmpty) {
        throw const HubException('Hub URL + code required.');
      }
      final result =
          await widget.api.redeem(hub: cleanHub, code: cleanCode, name: name);
      await widget.session.savePair(
          hub: cleanHub,
          token: result.token,
          deviceId: result.deviceId,
          name: name.trim().isEmpty ? 'phone' : name.trim());
      widget.onPaired();
    } on HubException catch (e) {
      setState(() => _error = e.message);
    } catch (e) {
      setState(() => _error = 'Pairing failed: $e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _redeemFromLink(String link) {
    final info = parsePairUri(link);
    if (info == null) {
      setState(() => _error =
          'That link is not a Jarvis pair link (jarvis://pair?hub=..&code=..).');
      return;
    }
    _redeem(info.hub, info.code,
        _name.text.trim().isEmpty ? 'phone' : _name.text.trim());
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('JARVIS LITE · PAIR WITH HUB')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              'On your Mac dashboard: MOBILE LINK → PAIR → scan the QR, '
              'or enter the 8-char code (single-use, 10 min).',
              style: TextStyle(color: Colors.grey),
            ),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              icon: const Icon(Icons.qr_code_scanner),
              label: Text(_scanning ? 'CLOSE SCANNER' : 'SCAN QR'),
              onPressed: _busy
                  ? null
                  : () => setState(() => _scanning = !_scanning),
            ),
            if (_scanning) ...[
              const SizedBox(height: 8),
              SizedBox(
                height: 260,
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(8),
                  child: MobileScanner(
                    onDetect: (capture) {
                      for (final bc in capture.barcodes) {
                        final raw = bc.rawValue;
                        if (raw == null) continue;
                        final info = parsePairUri(raw);
                        if (info != null) {
                          setState(() => _scanning = false);
                          _redeem(info.hub, info.code,
                              _name.text.trim().isEmpty ? 'phone' : _name.text.trim());
                          break;
                        }
                      }
                    },
                  ),
                ),
              ),
            ],
            const SizedBox(height: 12),
            TextField(
              controller: _name,
              decoration: const InputDecoration(
                  labelText: 'DEVICE NAME', hintText: 'e.g. Pixel 8'),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _link,
              decoration: const InputDecoration(
                  labelText: 'OR PASTE PAIR LINK',
                  hintText: 'jarvis://pair?hub=..&code=..'),
            ),
            const SizedBox(height: 8),
            OutlinedButton(
              onPressed: _busy ? null : () => _redeemFromLink(_link.text),
              child: const Text('REDEEM FROM LINK'),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _hub,
              keyboardType: TextInputType.url,
              autocorrect: false,
              decoration: const InputDecoration(
                  labelText: 'OR HUB URL', hintText: 'http://192.168.1.5:5001'),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _code,
              autocorrect: false,
              textCapitalization: TextCapitalization.characters,
              maxLength: 8,
              inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[A-Za-z0-9]'))],
              decoration: const InputDecoration(
                  labelText: 'PAIRING CODE', hintText: 'e.g. 7KQ2PX4M'),
            ),
            const SizedBox(height: 8),
            FilledButton(
              onPressed: _busy
                  ? null
                  : () => _redeem(_hub.text, _code.text, _name.text),
              child: Text(_busy ? '…' : 'REDEEM & CONNECT'),
            ),
            if (_error != null) ...[
              const SizedBox(height: 10),
              Text(_error!, style: const TextStyle(color: Colors.redAccent)),
            ],
          ],
        ),
      ),
    );
  }
}
