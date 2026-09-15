/// On-device intent router — pure Dart, zero deps, unit-tested.
///
/// Decides whether a line should stay OFFLINE (queued note) or go to
/// the hub as chat. Anything uncertain → chat: the hub brain is the
/// authority, the phone never guesses at actions.
///
/// Rules:
///   /note <text> | note: <text>  → offline note (works with no signal)
///   everything else               → hub chat (same pipeline as PWA)
enum LocalIntent { note, chat }

class RoutedIntent {
  final LocalIntent kind;
  final String text;
  const RoutedIntent(this.kind, this.text);
}

RoutedIntent routeInput(String raw) {
  final text = raw.trim();
  if (text.isEmpty) return const RoutedIntent(LocalIntent.chat, '');
  final lower = text.toLowerCase();
  for (final prefix in ['/note ', '/note\n', 'note:']) {
    if (lower.startsWith(prefix)) {
      return RoutedIntent(
          LocalIntent.note, text.substring(prefix.length).trim());
    }
  }
  if (lower == '/note' || lower == 'note:') {
    return const RoutedIntent(LocalIntent.note, '');
  }
  return RoutedIntent(LocalIntent.chat, text);
}
