import { useEffect, useState } from 'react';

import { api, UNAUTHORIZED_EVENT } from '../lib/api.js';
import { Button, Field, TextInput } from './Form.jsx';
import { Loading } from './State.jsx';

// Shows the dashboard once the API accepts it, and a sign-in screen while it
// answers 401: a host with an API key needs the key typed once per browser
// session, a host without one never asks.
export default function AuthGate({ children }) {
  const [status, setStatus] = useState('checking');

  useEffect(() => {
    let active = true;
    const onUnauthorized = () => setStatus('signed-out');
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    api
      .health()
      .then(() => {
        if (active) setStatus('signed-in');
      })
      .catch((err) => {
        // Any other failure is the pages' to show.
        if (active) setStatus(err?.status === 401 ? 'signed-out' : 'signed-in');
      });
    return () => {
      active = false;
      window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    };
  }, []);

  if (status === 'checking') return <Loading />;
  if (status === 'signed-out') return <SignIn onSignedIn={() => setStatus('signed-in')} />;
  return children;
}

function SignIn({ onSignedIn }) {
  const [apiKey, setApiKey] = useState('');
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await api.createSession(apiKey);
      onSignedIn();
    } catch (err) {
      setError(err);
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-lg border border-slate-200 bg-white p-6"
      >
        <h1 className="font-display text-xl font-semibold tracking-tight">Mash admin</h1>
        <Field label="API key" hint="The host's MASH_API_KEY.">
          <TextInput
            type="password"
            autoComplete="current-password"
            autoFocus
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
          />
        </Field>
        {error ? <p className="text-sm text-rose-700">{error.message}</p> : null}
        <Button type="submit" variant="primary" disabled={submitting || !apiKey.trim()}>
          Sign in
        </Button>
      </form>
    </div>
  );
}
