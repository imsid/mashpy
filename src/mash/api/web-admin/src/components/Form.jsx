export function Button({ variant = 'secondary', className = '', ...props }) {
  const base =
    'inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50';
  const variants = {
    primary: 'bg-slate-900 text-white hover:bg-slate-700',
    secondary: 'border border-slate-300 bg-white text-slate-700 hover:bg-slate-50',
    ghost: 'text-slate-500 hover:bg-slate-100',
    danger: 'border border-rose-300 bg-white text-rose-700 hover:bg-rose-50',
  };
  return <button className={`${base} ${variants[variant]} ${className}`} {...props} />;
}

export function Field({ label, hint, children }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>
      {children}
      {hint ? <span className="mt-1 block text-xs text-slate-400">{hint}</span> : null}
    </label>
  );
}

const inputBase =
  'w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm shadow-sm focus:border-slate-400 focus:outline-none focus:ring-1 focus:ring-slate-300';

export function TextInput({ className = '', ...props }) {
  return <input className={`${inputBase} ${className}`} {...props} />;
}

export function TextArea({ className = '', ...props }) {
  return <textarea className={`${inputBase} resize-y ${className}`} {...props} />;
}

export function Select({ children, className = '', ...props }) {
  return (
    <select className={`${inputBase} ${className}`} {...props}>
      {children}
    </select>
  );
}
