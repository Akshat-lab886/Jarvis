import 'package:flutter/material.dart';

import 'core/hub_api.dart';
import 'core/outbox_db.dart';
import 'core/session.dart';
import 'screens/chat_screen.dart';
import 'screens/pair_screen.dart';

/// Jarvis Lite — paired phone remote for the Jarvis Mac hub.
///
/// Flow: pair once (QR / paste-link / manual 8-char code) → per-device
/// Bearer token in the platform keychain → chat runs the SAME hub
/// pipeline (host-silent, HITL-gated), notes queue offline in SQLite.
///
/// 100% free: no accounts, no cloud, no App Store — debug APK via
/// GitHub Actions, side-loaded or PWA-equivalent install.
void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const JarvisLiteApp());
}

class JarvisLiteApp extends StatefulWidget {
  const JarvisLiteApp({super.key});

  @override
  State<JarvisLiteApp> createState() => _JarvisLiteAppState();
}

class _JarvisLiteAppState extends State<JarvisLiteApp> {
  final Session session = Session();
  late final HubApi api;
  final OutboxDb outbox = OutboxDb();
  bool _ready = false;

  @override
  void initState() {
    super.initState();
    api = HubApi(session);
    session.load().then((_) {
      if (mounted) setState(() => _ready = true);
    });
  }

  @override
  void dispose() {
    api.close();
    outbox.close();
    super.dispose();
  }

  void _onPaired() => setState(() {});
  void _onUnpaired() => setState(() {});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Jarvis Lite',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        brightness: Brightness.dark,
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFFFFB000),
          brightness: Brightness.dark,
          surface: const Color(0xFF0B0D0F),
        ),
        scaffoldBackgroundColor: const Color(0xFF050607),
        appBarTheme: const AppBarTheme(
          backgroundColor: Color(0xFF0B0D0F),
          foregroundColor: Color(0xFFFFB000),
        ),
      ),
      home: !_ready
          ? const Scaffold(
              body: Center(child: CircularProgressIndicator()))
          : session.isPaired
              ? ChatScreen(
                  session: session,
                  api: api,
                  outbox: outbox,
                  onUnpaired: _onUnpaired,
                )
              : PairScreen(
                  session: session,
                  api: api,
                  onPaired: _onPaired,
                ),
    );
  }
}
