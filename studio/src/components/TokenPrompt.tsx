import { KeyRound } from "lucide-react";
import { type FormEvent, useState } from "react";
import { useCore } from "../api/core";

/** Shown when the core requires a token and none (or a wrong one) is saved. */
export function TokenPrompt() {
  const { setToken } = useCore();
  const [value, setValue] = useState("");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (value.trim()) setToken(value.trim());
  };

  return (
    <div className="overlay">
      <form className="dialog" onSubmit={submit}>
        <KeyRound size={28} aria-hidden />
        <h2>Connect to PicnicPlace Core</h2>
        <p>
          This core requires an access token. Find it as <code>server.token</code> in the core's
          configuration file.
        </p>
        <input
          type="password"
          autoFocus
          placeholder="Access token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-label="Access token"
        />
        <button type="submit" className="button primary">
          Connect
        </button>
      </form>
    </div>
  );
}
