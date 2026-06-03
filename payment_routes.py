import hmac
import hashlib
import razorpay
import re
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from firebase_admin import firestore
from config import db, client, limiter, RAZORPAY_KEY_SECRET, app

payment_bp = Blueprint('payment', __name__)

@payment_bp.route('/create-order', methods=['POST'])
@limiter.limit("20 per minute")
def create_order():
    """
    Creates a Razorpay order. 
    Amount should be in the smallest currency unit (e.g., paise for INR).
    """
    if not db:
        app.logger.error("Firebase Database is not initialized. Pre-creating order is not possible.")
        return jsonify({"error": "Server Database is not initialized. Please verify configuration settings."}), 500

    try:
        data = request.get_json()
        currency = data.get('currency', 'INR')
        address = data.get('address')
        items = data.get('items', [])
        user_id = data.get('userId')

        # 1. Enforce Kerala delivery only
        if not address or not address.get('state') or address.get('state').strip().lower() != 'kerala':
            return jsonify({"error": "We currently only deliver to Kerala."}), 400

        if not user_id or not items:
            return jsonify({"error": "User ID and items are required"}), 400

        # 2. Securely calculate Subtotal from products in Firestore
        subtotal = 0.0
        for item in items:
            product_id = item.get('id')
            quantity = int(item.get('quantity', 1))
            if not product_id:
                continue
            p_doc = db.collection('products').document(product_id).get()
            if not p_doc.exists:
                return jsonify({"error": f"Product with ID {product_id} not found"}), 400
            p_data = p_doc.to_dict()
            
            price_raw = p_data.get('price', 0)
            if isinstance(price_raw, str):
                price_clean = re.sub(r'[^0-9.]', '', price_raw)
                price = float(price_clean) if price_clean else 0.0
            else:
                price = float(price_raw)
            
            subtotal += price * quantity

        # 3. Process and validate coupon
        discount = 0.0
        coupon_code = data.get('couponCode')
        if coupon_code:
            coupon_code = coupon_code.strip().upper()
            coupon_doc = db.collection('coupons').document(coupon_code).get()
            if coupon_doc.exists:
                c_data = coupon_doc.to_dict()
                if c_data.get('isActive', False):
                    # Check expiration
                    expires_at = c_data.get('expiresAt')
                    is_expired = False
                    if expires_at:
                        now = datetime.now(timezone.utc)
                        if expires_at.tzinfo is None:
                            now_naive = datetime.now()
                            if expires_at < now_naive:
                                is_expired = True
                        else:
                            if expires_at < now:
                                is_expired = True
                                
                    # Check usage limit
                    max_uses = c_data.get('maxUses')
                    used_count = c_data.get('usedCount', 0)
                    is_limit_reached = max_uses is not None and used_count >= max_uses
                    
                    # Check min purchase
                    min_purchase = float(c_data.get('minPurchase', 0))
                    
                    if not is_expired and not is_limit_reached and subtotal >= min_purchase:
                        if c_data.get('discountType') == 'percentage':
                            discount = subtotal * (float(c_data.get('discountValue', 0)) / 100.0)
                        elif c_data.get('discountType') == 'fixed':
                            discount = float(c_data.get('discountValue', 0))
                        discount = min(discount, subtotal)
                    else:
                        if is_expired:
                            return jsonify({"error": "Coupon has expired"}), 400
                        elif is_limit_reached:
                            return jsonify({"error": "Coupon usage limit reached"}), 400
                        elif subtotal < min_purchase:
                            return jsonify({"error": f"Minimum purchase of ₹{min_purchase} required"}), 400
                else:
                    return jsonify({"error": "Coupon is inactive"}), 400
            else:
                return jsonify({"error": "Invalid coupon code"}), 400

        # 4. Calculate Final Total
        delivery_charge = 50.0
        platform_fee = 20.0
        calculated_total = subtotal + delivery_charge + platform_fee - discount
        calculated_total = max(0.0, calculated_total)

        amount_paise = int(round(calculated_total * 100))
        if amount_paise < 100:
            return jsonify({"error": "Minimum amount must be 100 paise (1 INR)"}), 400

        # Create Razorpay Order
        order_params = {
            'amount': amount_paise,
            'currency': currency,
            'payment_capture': 1
        }

        try:
            order = client.order.create(data=order_params)
        except razorpay.errors.BadRequestError as e:
            return jsonify({"error": f"Razorpay bad request: {str(e)}"}), 400
        except razorpay.errors.ServerError as e:
            return jsonify({"error": f"Razorpay server error: {str(e)}"}), 500
        except Exception as e:
            error_str = str(e).lower()
            if "auth" in error_str or "unauthorized" in error_str:
                return jsonify({"error": "Razorpay authentication failed"}), 401
            raise e
        
        # Pre-create a 'Pending' order record in Firestore
        try:
            orders_ref = db.collection('orders')
            order_payload = {
                'userId': user_id,
                'items': items,
                'subtotal': subtotal,
                'deliveryCharge': delivery_charge,
                'platformFee': platform_fee,
                'discount': discount,
                'couponCode': coupon_code if coupon_code else None,
                'total': calculated_total,
                'address': address,
                'razorpayOrderId': order['id'],
                'status': 'Pending',
                'createdAt': firestore.SERVER_TIMESTAMP,
                'updatedAt': firestore.SERVER_TIMESTAMP
            }
            orders_ref.document().set(order_payload)
        except Exception as fe:
            print(f"Failed to pre-save pending order: {str(fe)}")
            app.logger.warning(f"Failed to pre-save pending order: {str(fe)}")

        return jsonify(order), 200

    except Exception as e:
        print(f"Order Creation Exception: {str(e)}")
        import traceback
        traceback.print_exc()
        app.logger.error(f"Order Creation Error: {str(e)}")
        return jsonify({"error": "Failed to create payment order"}), 500

@payment_bp.route('/verify-payment', methods=['POST'])
@limiter.limit("10 per minute")
def verify_payment():
    """
    Verifies the payment signature sent by Razorpay after a successful payment.
    """
    try:
        data = request.get_json()
        
        razorpay_order_id = data.get('razorpay_order_id')
        razorpay_payment_id = data.get('razorpay_payment_id')
        razorpay_signature = data.get('razorpay_signature')

        if not razorpay_order_id or not razorpay_payment_id or not razorpay_signature:
            return jsonify({"error": "Missing required signature fields"}), 400

        # Construct the signature verification string
        # pattern: order_id + "|" + payment_id
        msg = f"{razorpay_order_id}|{razorpay_payment_id}"
        
        # Verify signature using HMAC SHA256
        generated_signature = hmac.new(
            RAZORPAY_KEY_SECRET.encode(),
            msg.encode(),
            hashlib.sha256
        ).hexdigest()

        if generated_signature == razorpay_signature:
            # Payment Verified! 
            # Now find the pending order and finalize it atomically
            if not db:
                app.logger.error("Firebase Database is not initialized during payment verification.")
                return jsonify({"status": "failure", "message": "Database not initialized on server"}), 500

            try:
                @firestore.transactional
                def finalize_order(transaction, orders_ref, products_ref):
                    # Find order by razorpay_order_id
                    query = orders_ref.where('razorpayOrderId', '==', razorpay_order_id).limit(1).stream()
                    order_doc = None
                    for d in query: order_doc = d; break
                    
                    if not order_doc:
                        # If no pending order found, we might need to create one (fallback)
                        return "Order document not found for verification"

                    # To ensure the order is locked in the transaction, get it transactionally
                    order_ref = orders_ref.document(order_doc.id)
                    order_snap = order_ref.get(transaction=transaction)
                    if not order_snap.exists:
                        return "Order document missing"

                    order_data = order_snap.to_dict()
                    if order_data.get('status') != 'Pending':
                        return "Order already processed"

                    # --- 1. PERFORM ALL READS ---
                    
                    # Read products
                    product_updates = []
                    for item in order_data.get('items', []):
                        pid = item.get('id')
                        try:
                            qty = int(item.get('quantity', 1))
                        except (ValueError, TypeError):
                            qty = 1
                        if not pid: continue
                        
                        p_ref = products_ref.document(pid)
                        p_snap = p_ref.get(transaction=transaction)
                        if not p_snap.exists: raise Exception(f"Product {pid} missing")
                        
                        try:
                            cur_stock = int(p_snap.get('stock') or 0)
                        except (ValueError, TypeError):
                            cur_stock = 0
                            
                        if cur_stock < qty: raise Exception(f"Stock exhausted for {p_snap.get('name')}")
                        
                        product_updates.append((p_ref, cur_stock - qty))

                    # Read coupon
                    coupon_update = None
                    coupon_code = order_data.get('couponCode')
                    if coupon_code:
                        coupon_ref = db.collection('coupons').document(coupon_code)
                        coupon_snap = coupon_ref.get(transaction=transaction)
                        if coupon_snap.exists:
                            current_uses = coupon_snap.get('usedCount') or 0
                            coupon_update = (coupon_ref, current_uses + 1)

                    # --- 2. PERFORM ALL WRITES ---
                    
                    # Update products
                    for p_ref, new_stock in product_updates:
                        transaction.update(p_ref, {'stock': new_stock})

                    # Update order to 'Processing'
                    transaction.update(order_ref, {
                        'status': 'Processing',
                        'paymentId': razorpay_payment_id,
                        'updatedAt': firestore.SERVER_TIMESTAMP
                    })

                    # Increment coupon uses
                    if coupon_update:
                        c_ref, new_uses = coupon_update
                        transaction.update(c_ref, {
                            'usedCount': new_uses,
                            'updatedAt': firestore.SERVER_TIMESTAMP
                        })

                    return {"status": "success", "order_data": order_data, "order_id": order_doc.id}

                res = finalize_order(db.transaction(), db.collection('orders'), db.collection('products'))
                
                if isinstance(res, dict) and res.get('status') == 'success':
                    # Shipping consignment is now handled by Saga-DTDC or a separate webhook.
                    # We just log the success here.
                    app.logger.info(f"Payment verified and order updated successfully. Order ID: {res.get('order_id')}")
                    return jsonify({"status": "success", "message": "Payment verified successfully"}), 200
                else:
                    app.logger.error(f"Failed to finalize order: {res}")
                    return jsonify({"status": "failure", "message": str(res)}), 400
            except Exception as fe:
                app.logger.error(f"Failed to finalize order on verification: {str(fe)}")
                return jsonify({"status": "failure", "message": f"Failed to record order: {str(fe)}"}), 500
        else:
            return jsonify({"status": "failure", "message": "Signature verification failed"}), 400

    except Exception as e:
        app.logger.error(f"Payment Verification Error: {str(e)}")
        return jsonify({"error": "Payment verification system error"}), 500

@payment_bp.route('/refund-payment', methods=['POST'])
@limiter.limit("10 per minute")
def refund_payment():
    """
    Issues a refund for a payment via Razorpay.
    """
    try:
        data = request.get_json()
        payment_id = data.get('paymentId')
        amount = data.get('amount') # Optional: Amount in rupees. If not provided, full refund is issued.
        
        if not payment_id:
            return jsonify({"error": "Payment ID is required"}), 400

        # Construct refund data
        refund_data = {
            "payment_id": payment_id
        }
        
        # If amount is provided, convert to paise for Razorpay
        if amount:
            refund_data["amount"] = int(float(amount) * 100)

        # Issue Refund via Razorpay Client
        refund = client.payment.refund(payment_id, refund_data)
        
        return jsonify({
            "status": "success",
            "message": "Refund issued successfully",
            "refund_id": refund.get('id'),
            "details": refund
        }), 200

    except Exception as e:
        app.logger.error(f"Refund Error: {str(e)}")
        # Check for specific Razorpay errors
        error_msg = str(e)
        if "already been refunded" in error_msg:
            return jsonify({"error": "This payment has already been refunded"}), 400
        return jsonify({"error": f"Failed to issue refund: {error_msg}"}), 500
