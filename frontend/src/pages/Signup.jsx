import { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import api from '../api'

export default function Signup() {
  const [form, setForm] = useState({ name: '', email: '', password: '' })
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()

  const handleSubmit = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      const res = await api.post('/auth/signup', form)
      localStorage.setItem('token', res.data.token)
      localStorage.setItem('name', res.data.name)
      navigate('/dashboard')
    } catch (err) {
      setError(err.response?.data?.detail || 'Signup failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={styles.container}>
      {/* Left panel */}
      <div style={styles.leftPanel}>
        <div style={styles.blob1} />
        <div style={styles.blob2} />
        <div style={styles.leftContent}>
          <div style={styles.logoMark}>
            <span style={styles.logoIcon}>⚡</span>
          </div>
          <h1 style={styles.brandName}>JobBot</h1>
          <p style={styles.brandTagline}>Your AI-powered<br />job hunting partner.</p>
          <div style={styles.statsRow}>
            <div style={styles.statBox}>
              <div style={styles.statNum}>10x</div>
              <div style={styles.statLabel}>Faster applying</div>
            </div>
            <div style={styles.statBox}>
              <div style={styles.statNum}>24/7</div>
              <div style={styles.statLabel}>Always watching</div>
            </div>
            <div style={styles.statBox}>
              <div style={styles.statNum}>AI</div>
              <div style={styles.statLabel}>Smart matching</div>
            </div>
          </div>
        </div>
      </div>

      {/* Right panel */}
      <div style={styles.rightPanel}>
        <div style={styles.formWrapper}>
          <div className="fade-in" style={styles.formHeader}>
            <h2 style={styles.formTitle}>Create your account</h2>
            <p style={styles.formSubtitle}>Start applying smarter today — it's free</p>
          </div>

          <form onSubmit={handleSubmit} style={styles.form}>
            <div className="fade-in-1" style={styles.fieldGroup}>
              <label style={styles.label}>Full name</label>
              <input
                style={styles.input}
                placeholder="Jane Smith"
                value={form.name}
                onChange={e => setForm({ ...form, name: e.target.value })}
                required
              />
            </div>

            <div className="fade-in-2" style={styles.fieldGroup}>
              <label style={styles.label}>Email address</label>
              <input
                style={styles.input}
                type="email"
                placeholder="jane@example.com"
                value={form.email}
                onChange={e => setForm({ ...form, email: e.target.value })}
                required
              />
            </div>

            <div className="fade-in-3" style={styles.fieldGroup}>
              <label style={styles.label}>Password</label>
              <input
                style={styles.input}
                type="password"
                placeholder="Create a strong password"
                value={form.password}
                onChange={e => setForm({ ...form, password: e.target.value })}
                required
              />
            </div>

            {error && (
              <div className="fade-in" style={styles.errorBox}>
                <span>⚠</span> {error}
              </div>
            )}

            <button className="fade-in-4" style={styles.button} type="submit" disabled={loading}>
              <span style={styles.btnInner}>
                {loading ? (
                  <>
                    <span style={styles.spinner} />
                    Creating account...
                  </>
                ) : (
                  <>Get started free <span>→</span></>
                )}
              </span>
            </button>
          </form>

          <p className="fade-in-5" style={styles.switchLink}>
            Already have an account?{' '}
            <Link to="/login" style={styles.link}>Sign in</Link>
          </p>
        </div>
      </div>
    </div>
  )
}

const styles = {
  container: {
    minHeight: '100vh',
    display: 'flex',
    fontFamily: "'Inter', sans-serif",
  },
  leftPanel: {
    width: '42%',
    background: 'linear-gradient(145deg, #4c1d95 0%, #7c3aed 50%, #c084fc 100%)',
    backgroundSize: '200% 200%',
    animation: 'gradientShift 8s ease infinite',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    position: 'relative',
    overflow: 'hidden',
  },
  blob1: {
    position: 'absolute', top: '-100px', right: '-60px',
    width: '350px', height: '350px', borderRadius: '50%',
    background: 'rgba(255,255,255,0.06)', pointerEvents: 'none',
  },
  blob2: {
    position: 'absolute', bottom: '-80px', left: '-80px',
    width: '300px', height: '300px', borderRadius: '50%',
    background: 'rgba(255,255,255,0.06)', pointerEvents: 'none',
  },
  leftContent: {
    position: 'relative', zIndex: 1,
    padding: '48px', color: '#fff',
  },
  logoMark: {
    width: '60px', height: '60px',
    background: 'rgba(255,255,255,0.2)',
    borderRadius: '16px',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    marginBottom: '24px',
    backdropFilter: 'blur(8px)',
    border: '1px solid rgba(255,255,255,0.25)',
  },
  logoIcon: { fontSize: '28px' },
  brandName: {
    fontSize: '40px', fontWeight: '800', color: '#fff',
    letterSpacing: '-1px', marginBottom: '12px',
  },
  brandTagline: {
    fontSize: '22px', fontWeight: '300', color: 'rgba(255,255,255,0.85)',
    lineHeight: '1.5', marginBottom: '40px',
  },
  statsRow: { display: 'flex', gap: '16px' },
  statBox: {
    background: 'rgba(255,255,255,0.15)',
    borderRadius: '12px',
    padding: '16px 20px',
    textAlign: 'center',
    backdropFilter: 'blur(8px)',
    border: '1px solid rgba(255,255,255,0.2)',
    flex: 1,
  },
  statNum: { fontSize: '22px', fontWeight: '800', color: '#fff', marginBottom: '4px' },
  statLabel: { fontSize: '11px', color: 'rgba(255,255,255,0.75)', fontWeight: '500', lineHeight: '1.3' },
  rightPanel: {
    flex: 1,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: '#fff', padding: '40px 24px',
  },
  formWrapper: { width: '100%', maxWidth: '400px' },
  formHeader: { marginBottom: '32px' },
  formTitle: {
    fontSize: '28px', fontWeight: '700', color: '#1e1b4b',
    letterSpacing: '-0.5px', marginBottom: '8px',
  },
  formSubtitle: { fontSize: '15px', color: '#6b7280' },
  form: { display: 'flex', flexDirection: 'column', gap: '18px' },
  fieldGroup: { display: 'flex', flexDirection: 'column', gap: '6px' },
  label: { fontSize: '13px', fontWeight: '600', color: '#374151', letterSpacing: '0.02em' },
  input: {
    padding: '13px 16px', borderRadius: '10px',
    border: '1.5px solid #e5e7eb', fontSize: '15px',
    color: '#1f2937', background: '#fafafa',
    transition: 'border-color 0.15s, box-shadow 0.15s', width: '100%',
  },
  errorBox: {
    background: '#fef2f2', border: '1px solid #fecaca',
    borderRadius: '8px', padding: '10px 14px',
    fontSize: '14px', color: '#dc2626',
    display: 'flex', alignItems: 'center', gap: '6px',
  },
  button: {
    padding: '14px', borderRadius: '10px',
    background: 'linear-gradient(135deg, #9333ea, #7c3aed)',
    color: '#fff', border: 'none', fontSize: '15px',
    fontWeight: '600', cursor: 'pointer',
    boxShadow: '0 4px 14px rgba(124, 58, 237, 0.35)',
    marginTop: '4px',
  },
  btnInner: {
    display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px',
  },
  spinner: {
    width: '16px', height: '16px',
    border: '2px solid rgba(255,255,255,0.3)',
    borderTopColor: '#fff', borderRadius: '50%',
    display: 'inline-block',
    animation: 'spin 0.7s linear infinite',
  },
  switchLink: {
    textAlign: 'center', marginTop: '28px',
    fontSize: '14px', color: '#6b7280',
  },
  link: { fontWeight: '600', color: '#9333ea' },
}
