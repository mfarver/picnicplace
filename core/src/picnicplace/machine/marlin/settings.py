"""How a machine profile tells the Marlin driver to talk to its firmware.

A profile's ``[marlin]`` table lists the G-code for connecting, homing, switching each
output, and reading each analog input. Firmware builds differ (for example, the LumenPnP's
stock firmware reads the vacuum sensors as 8 bits, and a patched build as 24 bits), so a
profile can also define named variants that replace parts of the input settings. The
machine's config picks a variant.
"""

import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarlinOutput(_Model):
    on: list[str] = Field(min_length=1)
    off: list[str] = Field(min_length=1)


class MarlinInput(_Model):
    read: list[str] = Field(
        min_length=1, description="Commands to send; the value is in the last one's reply."
    )
    pattern: str = Field(
        description="Regular expression with a 'value' group, matched against the reply."
    )
    sign_bits: int | None = Field(
        default=None,
        ge=2,
        le=64,
        description="Treat the raw value as a two's-complement number of this many bits.",
    )
    scale: float = Field(default=1.0, description="Multiply the (signed) value by this.")

    @model_validator(mode="after")
    def _check_pattern(self) -> Self:
        try:
            compiled = re.compile(self.pattern)
        except re.error as e:
            raise ValueError(f"invalid pattern {self.pattern!r}: {e}") from e
        if "value" not in compiled.groupindex:
            raise ValueError(f"pattern {self.pattern!r} needs a (?P<value>...) group")
        return self


class MarlinInputOverride(_Model):
    read: list[str] | None = None
    pattern: str | None = None
    sign_bits: int | None = Field(default=None, ge=2, le=64)
    scale: float | None = None
    clear_sign_bits: bool = Field(
        default=False, description="Remove the base input's sign_bits instead of setting it."
    )


class MarlinVariant(_Model):
    description: str
    inputs: dict[str, MarlinInputOverride] = Field(default_factory=dict[str, MarlinInputOverride])


class MarlinSettings(_Model):
    connect: list[str] = Field(
        default_factory=list[str], description="Sent after the firmware answers M115."
    )
    home: list[str] = Field(default_factory=lambda: ["G28"])
    estop: str = Field(default="M112", description="Immediate stop; firmware halts.")
    stop: str = Field(default="M410", description="Quick stop: end motion, stay running.")
    outputs: dict[str, MarlinOutput] = Field(default_factory=dict[str, MarlinOutput])
    inputs: dict[str, MarlinInput] = Field(default_factory=dict[str, MarlinInput])
    default_variant: str | None = None
    variants: dict[str, MarlinVariant] = Field(default_factory=dict[str, MarlinVariant])
    rs485: str | None = Field(
        default=None,
        description=(
            "Command that sends a hex packet on the feeder bus, such as M485. The reply is "
            "a line 'rs485-reply: <hex>', or 'rs485-reply: TIMEOUT'."
        ),
    )
    expected_machine_type: str | None = Field(
        default=None, description="MACHINE_TYPE that M115 should report; others get a warning."
    )

    @model_validator(mode="after")
    def _check_variants(self) -> Self:
        if self.default_variant is not None and self.default_variant not in self.variants:
            raise ValueError(f"default_variant {self.default_variant!r} is not defined")
        for name, variant in self.variants.items():
            for input_id in variant.inputs:
                if input_id not in self.inputs:
                    raise ValueError(f"variant {name!r} changes unknown input {input_id!r}")
        return self

    def resolved(self, variant: str | None) -> "MarlinSettings":
        """These settings with a variant applied (or the default variant, if any)."""
        name = variant or self.default_variant
        if name is None:
            return self
        chosen = self.variants.get(name)
        if chosen is None:
            known = ", ".join(sorted(self.variants)) or "none"
            raise ValueError(f"unknown firmware variant {name!r}; this profile has: {known}")
        inputs = dict(self.inputs)
        for input_id, change in chosen.inputs.items():
            update = change.model_dump(exclude_none=True, exclude={"clear_sign_bits"})
            if change.clear_sign_bits:
                update["sign_bits"] = None
            inputs[input_id] = inputs[input_id].model_copy(update=update)
        return self.model_copy(update={"inputs": inputs, "variants": {}, "default_variant": None})
