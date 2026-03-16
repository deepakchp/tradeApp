# cython: boundscheck=False, wraparound=False, cdivision=True
# modules/fast_greeks.pyx

from libc.math cimport exp, log, sqrt, M_PI

cdef double M_SQRT2PI = sqrt(2.0 * M_PI)

cdef double norm_pdf(double x) noexcept nogil:
    return exp(-0.5 * x * x) / M_SQRT2PI

cdef double norm_cdf(double x) noexcept nogil:
    """
    Fast polynomial approximation for the standard normal CDF.
    Abramowitz and Stegun formula 26.2.17.
    Max error ~ 7.5e-8
    """
    cdef double b1 =  0.319381530
    cdef double b2 = -0.356563782
    cdef double b3 =  1.781477937
    cdef double b4 = -1.821255978
    cdef double b5 =  1.330274429
    cdef double p  =  0.2316419
    cdef double c  =  0.39894228
    
    cdef int sign = 1
    if x < 0:
        sign = -1
        x = -x

    cdef double t = 1.0 / (1.0 + p * x)
    cdef double result = 1.0 - c * exp(-x * x / 2.0) * t * (
        b1 + t * (b2 + t * (b3 + t * (b4 + t * b5)))
    )
    
    if sign == -1:
        return 1.0 - result
    return result

cdef void c_d1_d2(double S, double K, double T, double sigma, double r, double q, double* d1_out, double* d2_out) noexcept nogil:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        d1_out[0] = 0.0
        d2_out[0] = 0.0
        return
        
    cdef double denom = sigma * sqrt(T)
    cdef double d1 = (log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / denom
    cdef double d2 = d1 - denom
    d1_out[0] = d1
    d2_out[0] = d2

cpdef double price(double S, double K, double T, double sigma, str option_type, double r=0.06, double q=0.0) noexcept:
    """
    Calculates the BSM option price. 
    """
    if T <= 0:
        if option_type == "CE":
            return max(S - K, 0.0)
        return max(K - S, 0.0)

    if sigma <= 0:
        if option_type == "CE":
            return max(S * exp(-q * T) - K * exp(-r * T), 0.0)
        return max(K * exp(-r * T) - S * exp(-q * T), 0.0)

    cdef double d1, d2
    c_d1_d2(S, K, T, sigma, r, q, &d1, &d2)
    
    if option_type == "CE":
        return S * exp(-q * T) * norm_cdf(d1) - K * exp(-r * T) * norm_cdf(d2)
    else:
        return K * exp(-r * T) * norm_cdf(-d2) - S * exp(-q * T) * norm_cdf(-d1)

cpdef dict greeks(double S, double K, double T, double sigma, str option_type, double r=0.06, double q=0.0):
    """
    Calculates the full set of BSM Greeks.
    Returns: {"delta": float, "gamma": float, "theta": float, "vega": float, "vanna": float}
    """
    if T <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "vanna": 0.0}

    cdef double d1, d2
    c_d1_d2(S, K, T, sigma, r, q, &d1, &d2)

    cdef double sqrt_T = sqrt(T)
    cdef double pdf_d1 = norm_pdf(d1)
    cdef double exp_qT = exp(-q * T)
    
    cdef double delta, theta, vega, gamma, vanna
    
    if option_type == "CE":
        delta = exp_qT * norm_cdf(d1)
    else:
        delta = exp_qT * (norm_cdf(d1) - 1.0)
        
    gamma = pdf_d1 / (S * sigma * sqrt_T)
    
    cdef double theta_common = -(S * exp_qT * pdf_d1 * sigma) / (2.0 * sqrt_T)
    if option_type == "CE":
        theta = (theta_common + q * S * exp_qT * norm_cdf(d1) - r * K * exp(-r * T) * norm_cdf(d2)) / 365.0
    else:
        theta = (theta_common - q * S * exp_qT * norm_cdf(-d1) + r * K * exp(-r * T) * norm_cdf(-d2)) / 365.0

    vega = S * exp_qT * pdf_d1 * sqrt_T / 100.0
    vanna = -pdf_d1 * d2 / sigma

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega":  round(vega, 4),
        "vanna": round(vanna, 4)
    }

cpdef double implied_vol(double S, double K, double T, double market_price, str option_type, double r=0.06, double q=0.0, double tol=1e-6, int max_iter=200) noexcept:
    """
    Newton-Raphson fast IV solver.
    """
    if T <= 0 or market_price <= 0:
        return 0.0
        
    cdef double sigma = 0.25
    cdef int i
    cdef double p, v, diff
    cdef double d1, d2
    
    for i in range(max_iter):
        p = price(S, K, T, sigma, option_type, r, q)
        # We need raw vega for Newton Raphson, so we multiply by 100
        # since our greeks() method returns 1% scaled vega
        c_d1_d2(S, K, T, sigma, r, q, &d1, &d2)
        v = S * exp(-q * T) * norm_pdf(d1) * sqrt(T) 
        
        diff = p - market_price
        
        if abs(diff) < tol:
            break
            
        if v < 1e-10:
            break
            
        sigma = sigma - diff / v
        
        if sigma < 0.001:
            sigma = 0.001
        elif sigma > 10.0:
            sigma = 10.0
            
    return round(sigma, 6)
