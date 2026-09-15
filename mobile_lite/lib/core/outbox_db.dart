import 'dart:convert';

import 'package:path/path.dart' as p;
import 'package:sqflite/sqflite.dart';

/// Offline outbox (SQLite) — mirrors the PWA's localStorage queue.
///
/// Notes saved with no connection wait here and push on SYNC.
/// Cap: 200 rows (oldest dropped first), 2000 chars per note —
/// same bounds as static/mobile.js so both clients behave alike.
/// Hub batches cap at 100 events, so [peekBatch] never exceeds that.
class OutboxDb {
  static const maxRows = 200;
  static const maxBatch = 100;
  static const maxText = 2000;

  Database? _db;

  Future<Database> open() async {
    if (_db != null) return _db!;
    final dir = await getDatabasesPath();
    _db = await openDatabase(
      p.join(dir, 'jarvis_lite.db'),
      version: 1,
      onCreate: (db, _) async {
        await db.execute('CREATE TABLE outbox('
            'id TEXT PRIMARY KEY, type TEXT NOT NULL, '
            'payload TEXT NOT NULL, created INTEGER NOT NULL)');
        await db.execute('CREATE TABLE hub_events('
            'id TEXT PRIMARY KEY, type TEXT NOT NULL, '
            'summary TEXT NOT NULL, created INTEGER NOT NULL)');
      },
    );
    return _db!;
  }

  /// Queue an offline memory note. Returns false when the text is empty.
  Future<bool> enqueueNote(
      {required String id,
      required String text,
      String category = 'note'}) async {
    final clean = text.trim();
    if (clean.isEmpty) return false;
    final db = await open();
    final count =
        Sqflite.firstIntValue(await db.rawQuery('SELECT COUNT(*) FROM outbox')) ??
            0;
    if (count >= maxRows) {
      await db.rawDelete(
          'DELETE FROM outbox WHERE id IN (SELECT id FROM outbox ORDER BY created ASC LIMIT 1)');
    }
    await db.insert(
        'outbox',
        {
          'id': id,
          'type': 'memory.remember',
          'payload': jsonEncode({
            'text': clean.substring(
                0, clean.length > maxText ? maxText : clean.length),
            'category': category,
            'tags': ['mobile'],
            'importance': 5,
          }),
          'created': DateTime.now().millisecondsSinceEpoch,
        },
        conflictAlgorithm: ConflictAlgorithm.ignore);
    return true;
  }

  /// Oldest-first batch for the next sync push (≤100).
  Future<List<Map<String, dynamic>>> peekBatch() async {
    final db = await open();
    final rows = await db.query('outbox',
        orderBy: 'created ASC', limit: maxBatch);
    return rows
        .map((r) => {
              'id': r['id'],
              'type': r['type'],
              'payload': jsonDecode(r['payload'] as String),
            })
        .toList();
  }

  /// Drop a pushed batch. The hub counts EVERY pushed event as applied
  /// or skipped, so the whole batch is acknowledged — same rule as the
  /// PWA (mobile.js doSync): skipped ids (dup/unknown) can never
  /// succeed on retry, and sync is idempotent by client UUID.
  Future<void> dropIds(Iterable<String> ids) async {
    final list = ids.toList();
    if (list.isEmpty) return;
    final db = await open();
    final batch = db.batch();
    for (final id in list) {
      batch.delete('outbox', where: 'id = ?', whereArgs: [id]);
    }
    await batch.commit(noResult: true);
  }

  Future<int> pendingCount() async {
    final db = await open();
    return Sqflite.firstIntValue(
            await db.rawQuery('SELECT COUNT(*) FROM outbox')) ??
        0;
  }

  /// Cache the latest hub events for the feed (keeps last 20).
  Future<void> cacheHubEvents(List<Map<String, dynamic>> events) async {
    if (events.isEmpty) return;
    final db = await open();
    final batch = db.batch();
    for (final e in events) {
      final type = (e['type'] ?? '').toString();
      final payload = (e['payload'] as Map?) ?? {};
      final summary = type == 'memory.remember'
          ? '✎ ${(payload['text'] ?? '').toString()}'.trim()
          : type == 'memory.forget'
              ? '⌫ forget: ${payload['keyword'] ?? payload['memory_id'] ?? ''}'
              : '• $type';
      batch.insert(
          'hub_events',
          {
            'id': (e['id'] ?? '').toString(),
            'type': type,
            'summary': summary.length > 160 ? summary.substring(0, 160) : summary,
            'created': DateTime.now().millisecondsSinceEpoch,
          },
          conflictAlgorithm: ConflictAlgorithm.ignore);
    }
    await batch.commit(noResult: true);
    await db.rawDelete('DELETE FROM hub_events WHERE id NOT IN '
        '(SELECT id FROM hub_events ORDER BY created DESC LIMIT 20)');
  }

  Future<List<String>> recentHubSummaries() async {
    final db = await open();
    final rows =
        await db.query('hub_events', orderBy: 'created DESC', limit: 20);
    return rows.map((r) => (r['summary'] ?? '').toString()).toList();
  }

  Future<void> close() async {
    await _db?.close();
    _db = null;
  }
}
