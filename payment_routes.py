import hmac
import hashlib
import razorpay
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
        amount = data.get('amount') # In rupees
        currency = data.get('currency', 'INR')

        if not amount:
            return jsonify({"error": "Amount is required"}), 400

        # Validate amount >= 100 paise (1 INR)
        try:
            amount_paise = int(float(amount) * 100)
        except ValueError:
            return jsonify({"error": "Invalid amount format"}), 400

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
        
        # Optional: Pre-create a 'Pending' order record in Firestore
        # This helps track abandoned checkouts and simplifies the verification flow
        if db and data.get('userId') and data.get('items'):
            try:
                orders_ref = db.collection('orders')
                order_payload = {
                    'userId': data.get('userId'),
                    'items': data.get('items'),
                    'total': amount,
                    'address': data.get('address'),
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

                    order_data = order_doc.to_dict()
                    if order_data.get('status') != 'Pending':
                        return "Order already processed"

                    # Atomic Stock Reduction
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
                        
                        transaction.update(p_ref, {'stock': cur_stock - qty})

                    # Update Order to 'Processing'
                    transaction.update(order_doc.reference, {
                        'status': 'Processing',
                        'paymentId': razorpay_payment_id,
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
