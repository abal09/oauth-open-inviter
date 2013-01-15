import urlparse
import urllib
import httplib2
import oauth2 as oauth
import simplejson as json
import datetime

from oauth_open_inviter.oauth_access.utils.anyetree import etree
from oauth_open_inviter.oauth_access.utils.multipart import get_headers_and_body
from oauth_open_inviter.oauth_access.utils import OAuth20Token, Client
from oauth_open_inviter.oauth_access.exceptions import NotAuthorized, ServiceFail, MissingToken

class BaseAccess(object):

    user_agent = None
    request_token_url = None
    access_token_url = None
    authorize_url = None
    scope_urls = []
    extra_auth_params = {}

    def __init__(self, consumer_key, consumer_secret,  *args, **kwargs):
        """
        Init function
        Keyword params:
            - **consumer_key** -- OAuth Consumer Key, should be generated by your OAuth provider
            - **consumer_secret** -- OAuth Consumer Secret Key, should be generated by your OAuth provider
            only for step 2:
            - **oauth_token** -- request token, got by `get_tokens` function while step 1
            - **oauth_token_secret** -- secret request token, got b
            y `get_tokens` function while step 1
            - **oauth_verifier** -- GET param that is passed to the server
                                    when user is redirected back from the content provider
        """
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.consumer = oauth.Consumer(consumer_key, consumer_secret)
        self.signature_method = oauth.SignatureMethod_HMAC_SHA1()
        for key, value in kwargs.items():
            setattr(self, key, value)

    def get_params(self, params=None):
        params = params or {}
        if self.scope_urls:
            params['scope'] = ' '.join(self.scope_urls)
        return urllib.urlencode(params)

    def get_auth_params(self):
        raise NotImplementedError()

    def get_tokens(self):
        raise NotImplementedError()

    def get_auth_url(self):
        raise NotImplementedError()

    def receive_access_tokens(self, http=None):
        raise NotImplementedError()

    def _make_api_call(self, kind, url, token, method="GET", headers=None, **kwargs):
        raise NotImplementedError()

    def make_api_call(self, kind, url, token=None, method="GET", headers=None, **kwargs):
        token = token or getattr(self, 'access_token', None)
        response, content = self._make_api_call(kind, url, token, method, headers, **kwargs)
        if response["status"] == "401":
            raise NotAuthorized()
        if not content:
            raise ServiceFail("no content")
        if kind == "raw":
            return content
        elif kind == "json":
            try:
                return json.loads(content)
            except ValueError:
                # @@@ might be better to return a uniform cannot parse
                # exception and let caller determine if it is service fail
                raise ServiceFail("JSON parse error", content)
        elif kind == "xml":
            return etree.ElementTree(etree.fromstring(content))
        else:
            raise Exception("unsupported API kind")


class OAuthAccess(BaseAccess):

    def get_auth_params(self):
        params = {'oauth_callback': self.callback_url}
        if self.extra_auth_params:
            params.update(self.extra_auth_params)
        return params

    def get_tokens(self):
        """
        Step 1
        Receives a request oauth_token and oauth_token_secret from an OAuth provider and returns it.
        Tokens should be saved and passed to the class at the step 2
        """
        client = oauth.Client(self.consumer)

        resp, content = client.request(
            "%s?%s" % (self.request_token_url, self.get_params(self.get_auth_params())),
            method="GET"
        )
        if resp['status'] != '200':
            raise Exception("Invalid response %s." % resp['status'])

        token = dict(urlparse.parse_qsl(content))
        self.oauth_token = token.get('oauth_token')
        self.oauth_token_secret = token.get('oauth_token_secret')
        return { 'oauth_token': self.oauth_token, 'oauth_token_secret': self.oauth_token_secret }

    def get_auth_url(self):
        """
        Step 1
        Returns an url for auth user redirecting
        """
        token = oauth.Token(self.oauth_token, self.oauth_token_secret)
        request = oauth.Request.from_consumer_and_token(
            self.consumer,
            token = token,
            http_url = self.authorize_url,
        )
        request.sign_request(self.signature_method, self.consumer, token)
        return request.to_url()

    def receive_access_tokens(self, http=None):
        """
        Step 2
        Receives final access tokens.
        """
        token = oauth.Token(self.oauth_token, self.oauth_token_secret)
        token.set_verifier(getattr(self, 'oauth_verifier', None))
        client = oauth.Client(self.consumer, token)
        resp, content = client.request(self.access_token_url, "GET")
        self.access_token = oauth.Token.from_string(content)
        return self.access_token

    def _make_api_call(self, kind, url, token, method="GET", headers=None, **kwargs):
        if isinstance(token, basestring):
            token = oauth.Token.from_string(token)
        client = Client(self.consumer, token=token)
        # @@@ LinkedIn requires Authorization header which is supported in
        # sub-classed version of Client (originally from oauth2)
        request_kwargs = dict(method=method, force_auth_header=True)
        if headers:
            request_kwargs['headers'] = headers
        if method == "POST":
            request_kwargs["body"] = urllib.urlencode(kwargs.get("params"))
        elif method =='GET':
            request_kwargs['parameters'] = kwargs.get('params')
        return client.request(url, **request_kwargs)


class OAuth2Access(OAuthAccess):

    def get_auth_params(self):
        params = dict(
            client_id = self.consumer_key,
            redirect_uri = self.callback_url,
            response_type = 'code'
        )
        if self.extra_auth_params:
            params.update(self.extra_auth_params)
        return params

    def get_tokens(self):
        return None

    def get_auth_url(self):
        """
        Step 1
        Returns an url for auth user redirecting
        """
        params = self.get_params(self.get_auth_params())
        return self.authorize_url + "?%s" % params

    def receive_access_tokens(self, http=None):
        """
        Exchange code
        """
        code = getattr(self, 'code', None)
        if code:
            body = self.get_params(dict(
                client_id = self.consumer_key,
                redirect_uri = self.callback_url,
                client_secret = self.consumer_secret,
                code = code,
                grant_type = 'authorization_code'
            ))
            headers = {
                'content-type': 'application/x-www-form-urlencoded',
                }

            if self.user_agent is not None:
                headers['user-agent'] = self.user_agent

            if http is None:
                http = httplib2.Http()

            resp, content = http.request(self.access_token_url, method='POST', body=body,
                headers=headers)

            try:
                d = json.loads(content)
            except StandardError:
                d = dict(urlparse.parse_qsl(content))

            # some providers respond with 'expires', others with 'expires_in'
            if d and 'expires' in d:
                d['expires_in'] = d.pop('expires')
            if resp.status == 200 and 'access_token' in d:
                access_token = d['access_token']
                refresh_token = d.get('refresh_token', None)
                token_expiry = None
                if 'expires_in' in d:
                    token_expiry = int(d['expires_in'])
                self.access_token = OAuth20Token(access_token, refresh_token, token_expiry)
                return self.access_token
            else:
                raise MissingToken

    def _make_api_call(self, kind, url, token, method="GET", headers=None, **kwargs):
        request_kwargs = dict(method=method)
        if headers:
            request_kwargs['headers'] = headers
        if method == "POST":
            params = {
                "access_token": str(token),
                }
            if kwargs.has_key('params'):
                params.update(kwargs["params"])

            if not kwargs.has_key('files'):
                request_kwargs["body"] = urllib.urlencode(params)
            else:
                headers, body = get_headers_and_body(fields=params, files=kwargs['files'])
                request_kwargs['headers'] = headers
                request_kwargs["body"] = body
        else:
            params = {
                "access_token": str(token),
                }
            if kwargs.has_key('params'):
                params.update(kwargs["params"])
            url += "?%s" % urllib.urlencode(params)
        http = httplib2.Http()
        return http.request(url, **request_kwargs)


