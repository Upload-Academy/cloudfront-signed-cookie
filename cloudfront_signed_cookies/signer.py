from os.path import exists
from json import dumps
from datetime import datetime, timedelta
from base64 import b64encode
from typing import Union
from re import match
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cloudfront_signed_cookies.errors import InvalidCloudFrontKeyId, InvalidCustomPolicy, PrivateKeyNotFound



class Signer:
    HASH_ALGORITHM = "SHA-384"

    def __init__(self, cloudfront_key_id: str, priv_key_file: str) -> None:
        """Initializes `Signer` object.

        Args:
            cloudfront_key_id(str): the ID assigned to the public key in CloudFront
            priv_key_file(str): the path to the private PEM-formatted key
        """
        if not match(r'^[0-9a-f]{8}\b-[0-9a-f]{4}\b-[0-9a-f]{4}\b-[0-9a-f]{4}\b-[0-9a-f]{12}$', cloudfront_key_id):
            raise InvalidCloudFrontKeyId("CloudFront public key ID must be a UUID string")
        else:
            self.cloudfront_key_id = cloudfront_key_id
        self.cloudfront_key_id: str = cloudfront_key_id
        if exists(priv_key_file):
            with open(priv_key_file, mode="rb") as priv_file:
                key_bytes = priv_file.read()
                self.priv_key = serialization.load_pem_private_key(
                    key_bytes, password=None
                )
        else:
            raise PrivateKeyNotFound(f"{priv_key_file} not found")

    def _sign(self, policy: str) -> bytes:
        """Generate signature from policy and the private key associated
        with the public key in the CloudFront trusted key group.
        """
        signature: bytes = self.priv_key.sign(
            data=policy.encode(),
            padding=padding.PSS(
                mgf=padding.MGF1(hashes.SHA384()), salt_length=padding.PSS.MAX_LENGTH
            ),
            algorithm=hashes.SHA384(),
        )
        return signature

    def _validate_custom_policy(self, policy: dict):
        """Validates custom policy for signed cookie.

        Custom policy must match the following schema:
        {
            "Statement": [
                {
                    "Resource": "URL of the file",
                    "Condition": {
                        "DateLessThan": {
                            "AWS:EpochTime": required ending date and time in Unix time format and UTC
                        },
                        "DateGreaterThan": {
                            "AWS:EpochTime":optional beginning date and time in Unix time format and UTC
                        },
                        "IpAddress": {
                            "AWS:SourceIp": "optional IP address"
                        }
                    }
                }
            ]
        }
        """
        conditions: dict = {}
        resource: str = ""
        allowed_condition_keys = ["DateLessThan", "DateGreaterThan", "IpAddress"]
        allowed_condition_key_subkeys = {
            "DateLessThan": "AWS:EpochTime",
            "DateGreaterThan": "AWS:EpochTime",
            "IpAddress": "AWS:SourceIp",
        }

        try:
            statements: Union[list, dict] = policy["Statement"]
        except KeyError:
            raise InvalidCustomPolicy("policy is missing Statement") from None
        if statements:

            def raise_missing_condition_exception():
                raise InvalidCustomPolicy(
                    "policy statement must have Condition block"
                ) from None

            if type(statements) == list:
                statement = statements[0]
                try:
                    conditions = statement["Condition"]
                    resource = statement["Resource"]
                except KeyError:
                    raise_missing_condition_exception()
            elif type(statements) == dict:
                try:
                    conditions = statements["Condition"]
                    resource = statements["Resource"]
                except KeyError:
                    raise_missing_condition_exception()
        else:
            raise InvalidCustomPolicy("policy statement is empty")

        for key in conditions:
            if key not in allowed_condition_keys:
                raise InvalidCustomPolicy(
                    f"invalid condition key: {key} "
                    "- key must be DateLessThan, DateGreaterThan, or IpAddress"
                )
            condition_key_value_type = type(conditions[key])
            if condition_key_value_type == dict:
                for sub_key in conditions[key]:
                    if sub_key != allowed_condition_key_subkeys[key]:
                        raise InvalidCustomPolicy(
                            f"invalid condition key sub-key found: {sub_key}"
                        )
                    condition_key_subkey_value_type: type = type(
                        conditions[key][sub_key]
                    )
                    if (
                        sub_key == "AWS:EpochTime"
                        and condition_key_subkey_value_type != int
                    ):
                        raise InvalidCustomPolicy(
                            "AWS:EpochTime value must be of type 'int'"
                            f", not {condition_key_subkey_value_type}"
                        )
                    else:
                        if (
                            sub_key == "IpAddress"
                            and condition_key_subkey_value_type != str
                        ):
                            raise InvalidCustomPolicy(
                                f"{sub_key} value must be of type 'str'"
                                f", not {condition_key_subkey_value_type}"
                            )
            else:
                raise InvalidCustomPolicy(
                    "condition key value must be of type 'dict'"
                    f", not {condition_key_value_type}"
                )

        resource_type: type = type(resource)
        if resource_type != str:
            raise TypeError(
                f"provided Resource must be of type 'str', not '{resource_type}'"
            )

    def _make_canned_policy(self, resource: str, expiration_date: int):
        """Returns default canned policy for signed cookies which only
        uses the `DataLessThan` condition.
        """
        policy = {
            "Statement": [
                {
                    "Resource": resource,
                    "Condition": {"DateLessThan": {"AWS:EpochTime": expiration_date}},
                }
            ]
        }
        return self._to_json(policy)

    def _sanitize_b64(self, s: str) -> str:
        """Removes invalid characters from final base64-encoded string.

        + -> -
        = -> _
        / -> ~
        """
        for k, v in {"+": "-", "=": "_", "/": "~"}.items():
            s = s.replace(k, v)
        return s

    def _to_json(self, s: dict) -> str:
        """Converts dict to JSON string stripped of whitespaces."""
        return dumps(s).replace(" ", "")

    def generate_cookies(
        self, Resource: str = "", Policy: dict = {}, SecondsBeforeExpires: int = 900
    ) -> dict:
        """Generate and return signed cookies for accessing content behind CloudFront.

        Args:
            Resource(str): base URL for the resource you want to allow access to
            Policy(dict): custom policy statement for signed cookie
            SecondsBeforeExpires(int): numbers of seconds before cookie expires,
                default=900 (15 minutes)

        Returns:
            dict: returns dict containing the CloudFront-Policy, CloudFront-Signature,
                and CloudFront-Key-Pair-Id cookies
        """
        if Policy:
            self._validate_custom_policy(Policy)
            policy: str = self._to_json(Policy)
        else:
            if not Resource:
                raise ValueError("must provide a resource URL")
            expires_on: datetime = datetime.now() + timedelta(
                seconds=SecondsBeforeExpires
            )
            policy: str = self._make_canned_policy(
                resource=Resource, expiration_date=int(expires_on.timestamp())
            )
        signature: bytes = self._sign(policy)

        encoded_policy = b64encode(policy.encode("utf8")).decode()
        encoded_signature = b64encode(signature).decode()
        cookies = {
            "CloudFront-Policy": self._sanitize_b64(encoded_policy),
            "CloudFront-Signature": self._sanitize_b64(encoded_signature),
            "CloudFront-Key-Pair-Id": self.cloudfront_key_id,
        }

        return cookies
