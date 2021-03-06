import pytest
import threading

from common import ProviderOptions, Ciphers, Curves, Protocols


class Provider(object):
    """
    A provider defines a specific provider of TLS. This could be
    S2N, OpenSSL, BoringSSL, etc.
    """

    ClientMode = "client"
    ServerMode = "server"

    def __init__(self, options: ProviderOptions):
        # If the test should wait for a specific output message before beginning,
        # put that message in ready_to_test_marker
        self.ready_to_test_marker = None

        # If the test should wait for a specific output message before sending
        # data, put that message in ready_to_send_input_marker
        self.ready_to_send_input_marker = None

        # Allows users to determine if the provider is ready to begin testing
        self._provider_ready_condition = threading.Condition()
        self._provider_ready = False

        if type(options) is not ProviderOptions:
            raise TypeError

        self.options = options
        if self.options.mode == Provider.ServerMode:
            self.cmd_line = self.setup_server()
        elif self.options.mode == Provider.ClientMode:
            self.cmd_line = self.setup_client()

    def setup_client(self):
        """
        Provider specific setup code goes here.
        This will probably include creating the command line based on ProviderOptions.
        """
        raise NotImplementedError

    def setup_server(self):
        """
        Provider specific setup code goes here.
        This will probably include creating the command line based on ProviderOptions.
        """
        raise NotImplementedError

    def get_cmd_line(self):
        return self.cmd_line

    def is_provider_ready(self):
        return self._provider_ready is True

    def set_provider_ready(self):
        with self._provider_ready_condition:
            self._provider_ready = True
            self._provider_ready_condition.notify()


class Tcpdump(Provider):
    """
    TcpDump is used by the dynamic record test. It only needs to watch
    a handful of packets before it can exit.

    This class still follows the provider setup, but all values are hardcoded
    because this isn't expected to be used outside of the dynamic record test.
    """
    def __init__(self, options: ProviderOptions):
        Provider.__init__(self, options)

    def setup_client(self):
        self.ready_to_test_marker = 'listening on lo'
        tcpdump_filter = "dst port {}".format(self.options.port)

        cmd_line = ["tcpdump",
            # Line buffer the output
            "-l",

            # Only read 10 packets before exiting. This is enough to find a large
            # packet, and still exit before the timeout.
            "-c", "10",

            # Watch the loopback device
            "-i", "lo",

            # Don't resolve IP addresses
            "-nn",

            # Set the buffer size to 1k
            "-B", "1024",
            tcpdump_filter]

        return cmd_line


class S2N(Provider):
    """
    The S2N provider translates flags into s2nc/s2nd command line arguments.
    """
    def __init__(self, options: ProviderOptions):
        self.ready_to_send_input_marker = None
        Provider.__init__(self, options)

    def setup_client(self):
        """
        Using the passed ProviderOptions, create a command line.
        """
        cmd_line = ['s2nc', '-e']

        # This is the last thing printed by s2nc before it is ready to send/receive data
        self.ready_to_send_input_marker = 'Cipher negotiated:'

        if self.options.use_session_ticket is False:
            cmd_line.append('-T')

        if self.options.insecure is True:
            cmd_line.append('--insecure')
        elif self.options.client_trust_store is not None:
            cmd_line.extend(['-f', self.options.client_trust_store])
        else:
            if self.options.cert is not None:
                cmd_line.extend(['-f', self.options.cert])

        if self.options.reconnect is True:
            cmd_line.append('-r')

        if self.options.protocol == Protocols.TLS13:
            cmd_line.append('--tls13')

        if self.options.cipher is not None:
            if self.options.cipher is Ciphers.KMS_PQ_TLS_1_0_2019_06:
                cmd_line.extend(['-c', 'KMS-PQ-TLS-1-0-2019-06'])
            elif self.options.cipher is Ciphers.PQ_SIKE_TEST_TLS_1_0_2019_11:
                cmd_line.extend(['-c', 'PQ-SIKE-TEST-TLS-1-0-2019-11'])
            else:
                cmd_line.extend(['-c', 'test_all'])
        else:
            cmd_line.extend(['-c', 'test_all'])

        if self.options.client_key_file:
            cmd_line.extend(['--key', self.options.client_key_file])
        if self.options.client_certificate_file:
            cmd_line.extend(['--cert', self.options.client_certificate_file])

        if self.options.extra_flags is not None:
            cmd_line.extend(self.options.extra_flags)

        cmd_line.extend([self.options.host, self.options.port])

        # Clients are always ready to connect
        self.set_provider_ready()

        return cmd_line

    def setup_server(self):
        """
        Using the passed ProviderOptions, create a command line.
        """
        cmd_line = ['s2nd', '-X', '--self-service-blinding']

        if self.options.key is not None:
            cmd_line.extend(['--key', self.options.key])
        if self.options.cert is not None:
            cmd_line.extend(['--cert', self.options.cert])

        if self.options.insecure is True:
            cmd_line.append('--insecure')

        if self.options.protocol == Protocols.TLS13:
            cmd_line.append('--tls13')

        if self.options.cipher is not None:
            cmd_line.extend(['-c', 'test_all'])

        if self.options.use_client_auth is True:
            cmd_line.append('-m')
            cmd_line.extend(['-t', self.options.client_certificate_file])

        if self.options.reconnects_before_exit is not None:
            cmd_line.append('--max-conns={}'.format(self.options.reconnects_before_exit))

        if self.options.extra_flags is not None:
            cmd_line.extend(self.options.extra_flags)

        cmd_line.extend([self.options.host, self.options.port])

        return cmd_line


class OpenSSL(Provider):

    supported_ciphers = {
        Ciphers.AES128_GCM_SHA256: 'TLS_AES_128_GCM_SHA256',
        Ciphers.AES256_GCM_SHA384: 'TLS_AES_256_GCM_SHA384',
        Ciphers.CHACHA20_POLY1305_SHA256: 'TLS_CHACHA20_POLY1305_SHA256',
    }

    def __init__(self, options: ProviderOptions):
        self.ready_to_send_input_marker = None
        Provider.__init__(self, options)

    def _join_ciphers(self, ciphers):
        """
        Given a list of ciphers, join the names with a ':' like OpenSSL expects
        """
        assert type(ciphers) is list

        cipher_list = []
        for c in ciphers:
            if c.min_version is Protocols.TLS13:
                cipher_list.append(OpenSSL.supported_ciphers[c])
            else:
                # This replace is only done for ciphers that are not TLS13 specific.
                # Run `openssl ciphers` to view the inconsistency in naming.
                cipher_list.append(c.name.replace("_", "-"))

        ciphers = ':'.join(cipher_list)

        return ciphers

    def _cipher_to_cmdline(self, protocol, cipher):
        cmdline = list()

        if cipher.min_version is Protocols.TLS13:
            cmdline.append('-ciphersuites')
        else:
            cmdline.append('-cipher')

        ciphers = []
        if type(cipher) is list:
            ciphers.append(self._join_ciphers(cipher))
        else:
            if cipher.min_version == Protocols.TLS13:
                ciphers.append(OpenSSL.supported_ciphers[cipher])
            else:
                # This replace is only done for ciphers that are not TLS13 specific.
                # Run `openssl ciphers` to view the inconsistency in naming.
                ciphers.append(cipher.name.replace("_", "-"))

        return cmdline + ciphers

    def setup_client(self):
        # s_client prints this message before it is ready to send/receive data
        self.ready_to_send_input_marker = 'Verify return code'

        cmd_line = ['openssl', 's_client']
        cmd_line.extend(['-connect', '{}:{}'.format(self.options.host, self.options.port)])

        # Additional debugging that will be captured incase of failure
        cmd_line.extend(['-debug', '-tlsextdebug'])

        if self.options.cert is not None:
            cmd_line.extend(['-cert', self.options.cert])
        if self.options.key is not None:
            cmd_line.extend(['-key', self.options.key])

        # Unlike s2n, OpenSSL allows us to be much more specific about which TLS
        # protocol to use.
        if self.options.protocol == Protocols.TLS13:
            cmd_line.append('-tls1_3')
        elif self.options.protocol == Protocols.TLS12:
            cmd_line.append('-tls1_2')
        elif self.options.protocol == Protocols.TLS11:
            cmd_line.append('-tls1_1')
        elif self.options.protocol == Protocols.TLS10:
            cmd_line.append('-tls1')

        if self.options.cipher is not None:
            cmd_line.extend(self._cipher_to_cmdline(self.options.protocol, self.options.cipher))

        if self.options.curve is not None:
            cmd_line.extend(['-curves', str(self.options.curve)])

        if self.options.use_client_auth is True:
            cmd_line.extend(['-key', self.options.client_key_file])
            cmd_line.extend(['-cert', self.options.client_certificate_file])

        if self.options.reconnect is True:
            cmd_line.append('-reconnect')

        if self.options.extra_flags is not None:
            cmd_line.extend(self.options.extra_flags)

        if self.options.server_name is not None:
            cmd_line.extend(['-servername', self.options.server_name])
            if self.options.verify_hostname is not None:
                cmd_line.extend(['-verify_hostname', self.options.server_name])

        # Clients are always ready to connect
        self.set_provider_ready()

        return cmd_line

    def setup_server(self):
        # s_server prints this message before it is ready to send/receive data
        self.ready_to_test_marker = 'ACCEPT'

        cmd_line = ['openssl', 's_server']
        cmd_line.extend(['-accept', '{}:{}'.format(self.options.host, self.options.port)])

        if self.options.reconnects_before_exit is not None:
            # If the user request a specific reconnection count, set it here
            cmd_line.extend(['-naccept', str(self.options.reconnects_before_exit)])
        else:
            # Exit after the first connection by default
            cmd_line.extend(['-naccept', '1'])

        # Additional debugging that will be captured incase of failure
        cmd_line.extend(['-debug', '-tlsextdebug'])

        cmd_line.append('-state')

        if self.options.cert is not None:
            cmd_line.extend(['-cert', self.options.cert])
        if self.options.key is not None:
            cmd_line.extend(['-key', self.options.key])

        # Unlike s2n, OpenSSL allows us to be much more specific about which TLS
        # protocol to use.
        if self.options.protocol == Protocols.TLS13:
            cmd_line.append('-tls1_3')
        elif self.options.protocol == Protocols.TLS12:
            cmd_line.append('-tls1_2')
        elif self.options.protocol == Protocols.TLS11:
            cmd_line.append('-tls1_1')
        elif self.options.protocol == Protocols.TLS10:
            cmd_line.append('-tls1')

        if self.options.cipher is not None:
            cmd_line.extend(self._cipher_to_cmdline(self.options.protocol, self.options.cipher))
            if self.options.cipher.parameters is not None:
                cmd_line.extend(['-dhparam', self.options.cipher.parameters])

        if self.options.curve is not None:
            cmd_line.extend(['-curves', str(self.options.curve)])
        if self.options.use_client_auth is True:
            cmd_line.extend(['-verify', '1'])

        return cmd_line


class BoringSSL(Provider):
    """
    NOTE: In order to focus on the general use of this framework, BoringSSL
    is not yet supported. The client works, the server has not yet been
    implemented, neither are in the default configuration.
    """
    def __init__(self, options: ProviderOptions):
        self.ready_to_send_input_marker = None
        Provider.__init__(self, options)

    def setup_server(self):
        pytest.skip('BoringSSL does not support server mode at this time')

    def setup_client(self):
        self.ready_to_send_input_marker = 'Cert issuer:'
        cmd_line = ['bssl', 's_client']
        cmd_line.extend(['-connect', '{}:{}'.format(self.options.host, self.options.port)])
        if self.options.cert is not None:
            cmd_line.extend(['-cert', self.options.cert])
        if self.options.key is not None:
            cmd_line.extend(['-key', self.options.key])
        if self.options.cipher is not None:
            if self.options.cipher == Ciphersuites.TLS_CHACHA20_POLY1305_SHA256:
                cmd_line.extend(['-cipher', 'TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256'])
            elif self.options.cipher == Ciphersuites.TLS_AES_128_GCM_256:
                pytest.skip('BoringSSL does not support Cipher {}'.format(self.options.cipher))
            elif self.options.cipher == Ciphersuites.TLS_AES_256_GCM_384:
                pytest.skip('BoringSSL does not support Cipher {}'.format(self.options.cipher))
        if self.options.curve is not None:
            if self.options.curve == Curves.P256:
                cmd_line.extend(['-curves', 'P-256'])
            elif self.options.curve == Curves.P384:
                cmd_line.extend(['-curves', 'P-384'])
            elif self.options.curve == Curves.X25519:
                pytest.skip('BoringSSL does not support curve {}'.format(self.options.curve))

        # Clients are always ready to connect
        self.set_provider_ready()

        return cmd_line


