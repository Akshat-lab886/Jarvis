// Jarvis Lite widget tests.
//
// Replaces the default counter-template test emitted by
// `flutter create` (it pumped a non-existent `MyApp` and could never
// pass). These stay fully offline: invalid input shows an error before
// any network call, so no hub, no platform channels, no mocking needed.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:jarvis_lite/core/hub_api.dart';
import 'package:jarvis_lite/core/session.dart';
import 'package:jarvis_lite/screens/pair_screen.dart';

Finder _fieldByLabel(String label) => find.byWidgetPredicate(
      (w) => w is TextField && w.decoration?.labelText == label,
    );

void main() {
  testWidgets('Pair screen offers QR, link, and manual entry',
      (WidgetTester tester) async {
    final session = Session();
    final api = HubApi(session);
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(
        home: PairScreen(session: session, api: api, onPaired: () {}),
      ),
    );

    expect(find.text('SCAN QR'), findsOneWidget);
    expect(find.text('REDEEM FROM LINK'), findsOneWidget);
    expect(find.text('REDEEM & CONNECT'), findsOneWidget);
    expect(_fieldByLabel('OR PASTE PAIR LINK'), findsOneWidget);
    expect(_fieldByLabel('OR HUB URL'), findsOneWidget);
    expect(_fieldByLabel('PAIRING CODE'), findsOneWidget);
  });

  testWidgets('Invalid pair link shows an error without networking',
      (WidgetTester tester) async {
    final session = Session();
    final api = HubApi(session);
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(
        home: PairScreen(session: session, api: api, onPaired: () {}),
      ),
    );

    await tester.enterText(
        _fieldByLabel('OR PASTE PAIR LINK'), 'not a pair link');
    await tester.tap(find.text('REDEEM FROM LINK'));
    await tester.pump();

    expect(find.textContaining('not a Jarvis pair link'), findsOneWidget);
  });

  testWidgets('Empty manual redeem asks for hub + code',
      (WidgetTester tester) async {
    final session = Session();
    final api = HubApi(session);
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(
        home: PairScreen(session: session, api: api, onPaired: () {}),
      ),
    );

    await tester.tap(find.text('REDEEM & CONNECT'));
    await tester.pump();

    expect(find.text('Hub URL + code required.'), findsOneWidget);
  });
}
