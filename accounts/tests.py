from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import TestCase
from django.test import RequestFactory
from django.utils import timezone
from unittest.mock import patch

from accounts.models import DailyUsage, UserSubscription
from accounts.subscription import can_run_analysis, get_usage_summary, record_analysis_run
from dcf_app.models import AnalysisRecord, WatchlistGroup, WatchlistItem


User = get_user_model()


class AccountsAuthTests(TestCase):
    password = 'IntrinsicTestPass-48291'

    def register_payload(self, email='Case.User@Example.com', **overrides):
        payload = {
            'first_name': ' Test ',
            'last_name': ' User ',
            'email': email,
            'password1': self.password,
            'password2': self.password,
        }
        payload.update(overrides)
        return payload

    def test_register_normalizes_email_and_names(self):
        response = self.client.post('/accounts/register/', self.register_payload())

        self.assertRedirects(response, '/')
        user = User.objects.get(email='case.user@example.com')
        self.assertEqual(user.first_name, 'Test')
        self.assertEqual(user.last_name, 'User')
        self.assertTrue(user.has_usable_password())
        self.assertEqual(user.subscription.plan, UserSubscription.PLAN_FREE)

    def test_register_rejects_duplicate_email_case_insensitive(self):
        User.objects.create_user(
            username='existing-user',
            email='case.user@example.com',
            password=self.password,
        )

        response = self.client.post('/accounts/register/', self.register_payload('CASE.USER@example.com'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ya existe una cuenta registrada con este email.')
        self.assertEqual(User.objects.filter(email__iexact='case.user@example.com').count(), 1)

    def test_register_rejects_blank_names_after_strip(self):
        response = self.client.post(
            '/accounts/register/',
            self.register_payload(first_name='   ', last_name='   '),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ingresá tu nombre.')
        self.assertContains(response, 'Ingresá tu apellido.')
        self.assertFalse(User.objects.exists())

    def test_login_accepts_email_case_insensitive(self):
        User.objects.create_user(
            username='case-user',
            email='case.user@example.com',
            password=self.password,
            first_name='Test',
        )

        response = self.client.post(
            '/accounts/login/',
            {'email': 'CASE.USER@example.com', 'password': self.password},
        )

        self.assertRedirects(response, '/')
        self.assertIn('_auth_user_id', self.client.session)

    def test_login_uses_generic_error_for_unknown_email_or_wrong_password(self):
        User.objects.create_user(
            username='case-user',
            email='case.user@example.com',
            password=self.password,
        )

        wrong_password = self.client.post(
            '/accounts/login/',
            {'email': 'case.user@example.com', 'password': 'WrongPass-12345'},
        )
        unknown_email = self.client.post(
            '/accounts/login/',
            {'email': 'missing@example.com', 'password': self.password},
        )

        self.assertContains(wrong_password, 'Email o contrasena incorrectos.')
        self.assertContains(unknown_email, 'Email o contrasena incorrectos.')

    def test_logout_requires_post_and_clears_session(self):
        user = User.objects.create_user(
            username='case-user',
            email='case.user@example.com',
            password=self.password,
        )
        self.client.force_login(user)

        get_response = self.client.get('/accounts/logout/')
        self.assertEqual(get_response.status_code, 405)
        self.assertIn('_auth_user_id', self.client.session)

        post_response = self.client.post('/accounts/logout/')
        self.assertRedirects(post_response, '/')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_navbar_changes_for_guest_and_authenticated_user(self):
        guest_response = self.client.get('/')
        self.assertContains(guest_response, 'Iniciar sesión')
        self.assertContains(guest_response, 'Crear cuenta')

        user = User.objects.create_user(
            username='case-user',
            email='case.user@example.com',
            password=self.password,
            first_name='Test',
        )
        self.client.force_login(user)
        authed_response = self.client.get('/')
        self.assertContains(authed_response, 'Hola, Test')
        self.assertContains(authed_response, 'Cerrar sesión')
        self.assertContains(authed_response, 'Mi cuenta')

    def test_account_home_requires_login(self):
        response = self.client.get('/accounts/')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])

    def test_account_home_shows_user_profile_and_usage(self):
        user = User.objects.create_user(
            username='account-user',
            email='account@example.com',
            password=self.password,
            first_name='Nicolas',
            last_name='Haugaard',
        )
        group = WatchlistGroup.objects.create(user=user, name='General')
        WatchlistItem.objects.create(watchlist=group, ticker='AAPL', company_name='Apple Inc.')
        WatchlistItem.objects.create(watchlist=group, ticker='MSFT', company_name='Microsoft')
        AnalysisRecord.objects.create(
            user=user,
            ticker='AAPL',
            company_name='Apple Inc.',
            metodo=AnalysisRecord.METODO_CAGR,
        )
        AnalysisRecord.objects.create(
            user=user,
            ticker='MSFT',
            company_name='Microsoft',
            metodo=AnalysisRecord.METODO_CAGR,
        )

        self.client.force_login(user)
        response = self.client.get('/accounts/')

        self.assertContains(response, 'Nicolas Haugaard')
        self.assertContains(response, 'account@example.com')
        self.assertContains(response, 'Free Beta')
        self.assertContains(response, 'Watchlists creadas')
        self.assertContains(response, 'Empresas en watchlist')
        self.assertContains(response, 'Análisis guardados')
        self.assertContains(response, 'Último análisis realizado')
        self.assertContains(response, 'MSFT')
        self.assertContains(response, '>1<', html=False)
        self.assertContains(response, '>2<', html=False)


class SubscriptionLimitTests(TestCase):
    password = 'IntrinsicTestPass-48291'

    def setUp(self):
        self.factory = RequestFactory()

    def request_for(self, user=None):
        request = self.factory.get('/app/')
        middleware = SessionMiddleware(lambda req: None)
        middleware.process_request(request)
        request.session.save()
        request.user = user or AnonymousUser()
        return request

    def test_guest_allows_three_analyses_then_blocks_fourth(self):
        request = self.request_for()

        for _ in range(3):
            self.assertTrue(can_run_analysis(request))
            record_analysis_run(request)

        self.assertFalse(can_run_analysis(request))
        summary = get_usage_summary(request)
        self.assertEqual(summary.plan, 'GUEST')
        self.assertEqual(summary.used, 3)
        self.assertEqual(summary.remaining, 0)

    def test_free_allows_fifteen_analyses_then_blocks_sixteenth(self):
        user = User.objects.create_user(username='free-user', email='free@example.com', password=self.password)
        request = self.request_for(user)

        for _ in range(15):
            self.assertTrue(can_run_analysis(request))
            record_analysis_run(request)

        self.assertFalse(can_run_analysis(request))
        summary = get_usage_summary(request)
        self.assertEqual(summary.plan, UserSubscription.PLAN_FREE)
        self.assertEqual(summary.used, 15)
        self.assertEqual(summary.remaining, 0)

    def test_pro_plan_is_unlimited(self):
        user = User.objects.create_user(username='pro-user', email='pro@example.com', password=self.password)
        UserSubscription.objects.update_or_create(user=user, defaults={'plan': UserSubscription.PLAN_PRO})
        request = self.request_for(user)

        for _ in range(25):
            self.assertTrue(can_run_analysis(request))
            record_analysis_run(request)

        summary = get_usage_summary(request)
        self.assertEqual(summary.plan, UserSubscription.PLAN_PRO)
        self.assertTrue(summary.is_unlimited)
        self.assertIsNone(summary.remaining)
        self.assertTrue(can_run_analysis(request))

    def test_admin_plan_cycles_counter_without_blocking(self):
        user = User.objects.create_user(
            username='admin-user',
            email='admin@example.com',
            password=self.password,
            is_staff=True,
        )
        request = self.request_for(user)

        for _ in range(15):
            self.assertTrue(can_run_analysis(request))
            record_analysis_run(request)

        summary_at_limit = get_usage_summary(request)
        self.assertEqual(summary_at_limit.plan, 'ADMIN')
        self.assertEqual(summary_at_limit.used, 15)
        self.assertEqual(summary_at_limit.remaining, 0)
        self.assertTrue(can_run_analysis(request))

        record_analysis_run(request)
        summary_after_reset = get_usage_summary(request)
        self.assertEqual(summary_after_reset.used, 1)
        self.assertEqual(summary_after_reset.remaining, 14)
        self.assertTrue(can_run_analysis(request))

    def test_invalid_ticker_does_not_consume_usage(self):
        with patch('dcf_app.views._check_ticker_eligibility', return_value='Ticker inválido') as eligibility:
            with patch('dcf_app.views._cached_ejecutar_dcf') as execute_dcf:
                response = self.client.get('/app/', {'ticker': 'BAD'})

        self.assertEqual(response.status_code, 200)
        eligibility.assert_called_once_with('BAD')
        execute_dcf.assert_not_called()
        usage = DailyUsage.objects.get(user__isnull=True)
        self.assertEqual(usage.analysis_count, 0)

    def test_guest_limit_blocks_dcf_execution(self):
        session = self.client.session
        session.save()
        DailyUsage.objects.create(
            session_key=session.session_key,
            date=timezone.localdate(),
            analysis_count=3,
        )

        with patch('dcf_app.views._check_ticker_eligibility', return_value=None):
            with patch('dcf_app.views._cached_ejecutar_dcf') as execute_dcf:
                response = self.client.get('/app/', {'ticker': 'AAPL'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Llegaste al límite gratuito diario')
        execute_dcf.assert_not_called()

    def test_user_usage_survives_logout_and_login(self):
        user = User.objects.create_user(
            username='returning-user',
            email='returning@example.com',
            password=self.password,
        )
        request = self.request_for(user)
        record_analysis_run(request)
        record_analysis_run(request)

        self.client.force_login(user)
        self.client.post('/accounts/logout/')
        login_response = self.client.post(
            '/accounts/login/',
            {'email': 'returning@example.com', 'password': self.password},
        )

        self.assertRedirects(login_response, '/')
        summary = get_usage_summary(self.request_for(user))
        self.assertEqual(summary.used, 2)
        self.assertEqual(summary.remaining, 13)
