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
      <div style={styles.card}>
        <h1 style={styles.title}>Create account</h1>
        <p style={styles.subtitle}>Start applying smarter</p>
        <form onSubmit={handleSubmit} style={styles.form}>
          <input style={styles.input} placeholder="Full name" value={form.name}
            onChange={e => setForm({...form, name: e.target.value})} required />
          <input style={styles.input} type="email" placeholder="Email" value={form.email}
            onChange={e => setForm({...form, email: e.target.value})} required />
          <input style={styles.input} type="password" placeholder="Password" value={form.password}
            onChange={e => setForm({...form, password: e.target.value})} required />
          {error && <p style={styles.error}>{error}</p>}
          <button style={styles.button} type="submit" disabled={loading}>
            {loading ? 'Creating account...' : 'Sign Up'}
          </button>
        </form>
        <p style={styles.link}>Already have an account? <Link to="/login">Log in</Link></p>
      </div>
    </div>
  )
}

const styles = {
  container: { minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#f5f5f5' },
  card: { background: '#fff', padding: '40px', borderRadius: '12px', width: '360px', boxShadow: '0 2px 20px rgba(0,0,0,0.08)' },
  title: { margin: 0, fontSize: '28px', fontWeight: '700', color: '#111' },
  subtitle: { color: '#888', marginTop: '6px', marginBottom: '28px' },
  form: { display: 'flex', flexDirection: 'column', gap: '12px' },
  input: { padding: '12px', borderRadius: '8px', border: '1px solid #ddd', fontSize: '15px', outline: 'none' },
  button: { padding: '13px', borderRadius: '8px', background: '#111', color: '#fff', border: 'none', fontSize: '15px', fontWeight: '600', cursor: 'pointer' },
  error: { color: '#e53e3e', fontSize: '14px', margin: 0 },
  link: { textAlign: 'center', marginTop: '20px', color: '#666', fontSize: '14px' }
}
