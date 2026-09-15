import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';

import '../core/hub_api.dart';
import '../core/intent.dart';
import '../core/outbox_db.dart';
import '../core/session.dart';

/// Chat view — shown once paired.
///
/// SEND → hub chat (same think→execute pipeline, host-silent,
/// HITL-gated). NOTE or `note:` / `/note` prefix → offline SQLite
/// outbox, pushed on SYNC. Handoff push folds a one-liner into hub
/// memory; handoff pull IS sync (push outbox + pull hub events).
class ChatScreen extends StatefulWidget {
  final Session session;
  final HubApi api;
  final OutboxDb outbox;
  final VoidCallback onUnpaired;

  const ChatScreen(
      {super.key,
      required this.session,
      required this.api,
      required this.outbox,
      required this.onUnpaired});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _Msg {
  final bool mine;
  final String text;
  final bool pending;
  _Msg(this.mine, this.text, {this.pending = false});
}

class _ChatScreenState extends State<ChatScreen> {
  final _input = TextEditingController();
  final _scroll = ScrollController();
  final List<_Msg> _msgs = [];
  final List<String> _hubFeed = [];
  bool _linked = true;
  bool _busy = false;
  String _status = '';

  @override
  void initState() {
    super.initState();
    _boot();
  }

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _boot() async {
    final cached = await widget.outbox.recentHubSummaries();
    if (!mounted) return;
    setState(() => _hubFeed.addAll(cached));
    _sync(silent: true);
  }

  void _add(_Msg m) {
    setState(() => _msgs.add(m));
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(_scroll.position.maxScrollExtent,
            duration: const Duration(milliseconds: 200),
            curve: Curves.easeOut);
      }
    });
  }

  Future<void> _send() async {
    final raw = _input.text;
    if (raw.trim().isEmpty || _busy) return;
    _input.clear();
    final routed = routeInput(raw);
    if (routed.kind == LocalIntent.note) {
      await _saveNote(routed.text);
      return;
    }
    _add(_Msg(true, routed.text));
    _add(_Msg(false, '…', pending: true));
    setState(() => _busy = true);
    try {
      final reply = await widget.api.chat(routed.text);
      setState(() {
        _msgs.removeLast();
        _linked = true;
      });
      _add(_Msg(false, reply));
    } on HubException catch (e) {
      setState(() {
        _msgs.removeLast();
        _linked = false;
        _status = e.message;
      });
      _add(_Msg(false, 'failed: ${e.message}'));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _saveNote(String text) async {
    final clean = text.trim();
    if (clean.isEmpty) {
      _add(_Msg(false, 'Empty note — nothing queued.'));
      return;
    }
    final ok = await widget.outbox.enqueueNote(id: const Uuid().v4(), text: clean);
    if (!ok) return;
    final pending = await widget.outbox.pendingCount();
    _add(_Msg(true, '✎ $clean'));
    _add(_Msg(false,
        pending == 1 ? 'queued offline — hit SYNC to push' : '$pending notes queued — hit SYNC to push'));
  }

  Future<void> _sync({bool silent = false}) async {
    if (_busy && !silent) return;
    if (!silent) setState(() => _busy = true);
    try {
      final batch = await widget.outbox.peekBatch();
      final res = await widget.api
          .sync(events: batch, cursor: widget.session.cursor);
      // Hub counts EVERY pushed event as applied/skipped → drop batch.
      await widget.outbox
          .dropIds(batch.map((e) => (e['id'] ?? '').toString()));
      await widget.session.saveCursor(res.cursor);
      await widget.outbox.cacheHubEvents(res.events);
      if (!mounted) return;
      setState(() {
        _linked = true;
        _status = '';
        for (final e in res.events) {
          final type = (e['type'] ?? '').toString();
          final payload = (e['payload'] as Map?) ?? {};
          final line = type == 'memory.remember'
              ? '✎ ${(payload['text'] ?? '').toString()}'
              : type == 'memory.forget'
                  ? '⌫ forget: ${payload['keyword'] ?? payload['memory_id'] ?? ''}'
                  : '• $type';
          _hubFeed.insert(0, line.length > 140 ? '${line.substring(0, 140)}…' : line);
        }
        if (_hubFeed.length > 20) {
          _hubFeed.removeRange(20, _hubFeed.length);
        }
      });
      if (!silent) {
        _add(_Msg(false, 'sync ok · +${res.applied} / cursor ${res.cursor}'));
      }
    } on HubException catch (e) {
      if (!mounted) return;
      setState(() {
        _linked = false;
        _status = e.message;
      });
      if (!silent) _add(_Msg(false, 'sync failed: ${e.message} (notes stay queued)'));
    } finally {
      if (!silent && mounted) setState(() => _busy = false);
    }
  }

  Future<void> _pushHandoff() async {
    final ctl = TextEditingController();
    final line = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Push handoff'),
        content: TextField(
            controller: ctl,
            autofocus: true,
            decoration: const InputDecoration(
                hintText: 'One-line context for the Mac hub')),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx),
              child: const Text('CANCEL')),
          FilledButton(
              onPressed: () => Navigator.pop(ctx, ctl.text),
              child: const Text('PUSH')),
        ],
      ),
    );
    if (line == null || line.trim().isEmpty) return;
    try {
      await widget.api.pushHandoff(line.trim());
      if (mounted) _add(_Msg(false, 'handoff pushed to hub memory.'));
    } on HubException catch (e) {
      if (mounted) _add(_Msg(false, 'handoff failed: ${e.message}'));
    }
  }

  Future<void> _unpair() async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Unpair this phone?'),
        content: const Text('Also REVOKE it on the Mac dashboard.'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('KEEP')),
          FilledButton(
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('UNPAIR')),
        ],
      ),
    );
    if (yes != true) return;
    await widget.session.clear();
    widget.onUnpaired();
  }

  @override
  Widget build(BuildContext context) {
    final pending = _msgs.where((m) => m.pending).length;
    return Scaffold(
      appBar: AppBar(
        title: const Text('JARVIS LITE'),
        actions: [
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12),
            child: Center(
              child: Text(_linked ? 'LINKED' : 'OFFLINE',
                  style: TextStyle(
                      color: _linked ? Colors.greenAccent : Colors.redAccent,
                      fontSize: 12)),
            ),
          ),
          PopupMenuButton<String>(
            onSelected: (v) {
              if (v == 'sync') _sync();
              if (v == 'pull') _sync();
              if (v == 'push') _pushHandoff();
              if (v == 'unpair') _unpair();
            },
            itemBuilder: (ctx) => const [
              PopupMenuItem(value: 'sync', child: Text('SYNC')),
              PopupMenuItem(value: 'pull', child: Text('PULL HANDOFF')),
              PopupMenuItem(value: 'push', child: Text('PUSH HANDOFF')),
              PopupMenuItem(value: 'unpair', child: Text('UNPAIR')),
            ],
          ),
        ],
      ),
      body: Column(
        children: [
          if (_status.isNotEmpty)
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(8),
              color: Colors.red.withValues(alpha: 0.15),
              child: Text(_status,
                  style: const TextStyle(color: Colors.redAccent, fontSize: 12)),
            ),
          if (_hubFeed.isNotEmpty)
            SizedBox(
              height: 90,
              child: ListView(
                padding: const EdgeInsets.symmetric(horizontal: 12),
                children: _hubFeed
                    .map((s) => Text(s,
                        style: const TextStyle(
                            color: Colors.grey, fontSize: 11)))
                    .toList(),
              ),
            ),
          Expanded(
            child: ListView.builder(
              controller: _scroll,
              padding: const EdgeInsets.all(12),
              itemCount: _msgs.length,
              itemBuilder: (ctx, i) {
                final m = _msgs[i];
                return Align(
                  alignment:
                      m.mine ? Alignment.centerRight : Alignment.centerLeft,
                  child: Container(
                    margin: const EdgeInsets.symmetric(vertical: 4),
                    padding: const EdgeInsets.all(10),
                    constraints: BoxConstraints(
                        maxWidth:
                            MediaQuery.of(ctx).size.width * (m.mine ? 0.88 : 0.92)),
                    decoration: BoxDecoration(
                      color: m.mine
                          ? const Color(0xFF111417)
                          : const Color(0xFF0B0D0F),
                      border: Border.all(
                          color: m.mine
                              ? const Color(0xFF333D48)
                              : const Color(0xFF1E252D)),
                      borderRadius: BorderRadius.circular(4),
                    ),
                    child: Opacity(
                      opacity: m.pending ? 0.6 : 1.0,
                      child: Text(m.text,
                          style: const TextStyle(
                              color: Color(0xFFE8EAED), fontSize: 14)),
                    ),
                  ),
                );
              },
            ),
          ),
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(8, 4, 8, 8),
              child: Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _input,
                      minLines: 1,
                      maxLines: 4,
                      textInputAction: TextInputAction.send,
                      onSubmitted: (_) => _send(),
                      decoration: const InputDecoration(
                          hintText: 'ask jarvis…  (/note … saves offline)',
                          border: OutlineInputBorder()),
                    ),
                  ),
                  const SizedBox(width: 6),
                  IconButton.filled(
                    icon: const Icon(Icons.send),
                    onPressed: _busy && pending > 0 ? null : _send,
                    tooltip: 'SEND',
                  ),
                  IconButton.outlined(
                    icon: const Icon(Icons.note_add),
                    onPressed: () async {
                      final t = _input.text;
                      _input.clear();
                      await _saveNote(routeInput(t).text);
                    },
                    tooltip: 'NOTE (offline)',
                  ),
                  IconButton.outlined(
                    icon: const Icon(Icons.sync),
                    onPressed: () => _sync(),
                    tooltip: 'SYNC',
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}
